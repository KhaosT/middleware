# Copyright (c) 2019 iXsystems, Inc.
# All rights reserved.
# This file is a part of TrueNAS
# and may not be copied and/or distributed
# without the express permission of iXsystems.

import contextlib
import glob
import os
import re
import shutil
import subprocess

from middlewared.schema import accepts
from middlewared.service import CallError, SystemServiceService, job

ASIGRA_DSOPDIR = '/usr/local/www/asigra'


class AsigraService(SystemServiceService):

    class Config:
        service = "asigra"
        service_verb = "restart"
        datastore = "services.asigra"
        datastore_prefix = "asigra_"

    @accepts()
    @job(lock='migrate_asigra')
    def migrate_to_plugin(self, job):
        # We want to perform migration here for asigra service which intends to transfer the existing asigra content
        # over to an asigra plugin.
        # Following steps are taken to ensure a smooth migration:
        # 1) It is ensured that asigra migration should be performed for the system in question.
        # 2) We check for the existence of custom jail image which should be present in the latest iso
        # 3) We setup iocage as needed and import the jail
        # 4) Let's configure asigra datasets to be renamed and mounted inside asigra plugin jail
        # 4a) Reword "Storage_Label.txt" in asigra dataset to correctly reflect the contents in asigra plugin
        # 4b) Destroy database/asigra contents in jail.
        # 4c) Rename asigra datasets to be under plugin root jail dataset
        # 4d) Change asigra datasets mountpoints to conform with how plugin expects them to be
        # 4e) We should set the ownership of the database correctly in the asigra dataset
        # 4f) Copy tmp directory to asigra plugin
        # 5) Destroy asigra dataset
        # 6) Set asigra filesystem value as None which indicates that migration has been performed or not needed

        asigra_config = self.middleware.call_sync('asigra.config')
        if not asigra_config['filesystem']:
            self.middleware.logger.debug('No migration required for the current system.')
            return

        system_asigra_path = os.path.join('/mnt', asigra_config['filesystem'])
        asigra_dataset = self.middleware.call_sync(
            'pool.dataset.query', [['id', '=', asigra_config['filesystem']]], {'get': True}
        )
        if len([
            c for c in asigra_dataset['children']
            if c['id'] in (os.path.join(asigra_config['filesystem'], d) for d in ('files', 'database', 'upgrade'))
        ]) != 3:
            raise CallError('Asigra not setup correctly. Aborting migration.')

        custom_jail_image = 'asigra_migration_image_9b5802df'
        custom_jail_image_path = glob.glob(f'/usr/local/share/asigra/{custom_jail_image}*')
        if not custom_jail_image_path:
            raise CallError('Custom asigra jail image does not exist.')
        else:
            custom_jail_image_path = custom_jail_image_path[0]

        try:
            pool = self.middleware.call_sync('jail.get_activated_pool')
        except Exception:
            # In this case let's activate the pool being used by asigra dataset right now
            pool = asigra_config['filesystem'].split('/', 1)[0]
            if not self.middleware.call_sync('jail.activate', pool):
                raise CallError(f'Failed to activate {pool} for iocage pool.')

        # Ensure iocage datasets exist
        self.middleware.call_sync('jail.check_dataset_existence')
        job.set_progress(10, f'{pool} pool activated for iocage.')

        import_job = self.middleware.call_sync(
            'jail.import_image', {'jail': custom_jail_image, 'path': custom_jail_image_path.rsplit('/', 1)[0]}
        )
        import_job.wait_sync()
        if import_job.error:
            raise CallError(f'Importing custom jail image failed: {import_job.error}')

        if not self.middleware.call_sync('plugin.query', [['id', '=', custom_jail_image]]):
            raise CallError(f'Plugin jail {custom_jail_image} not found.')

        job.set_progress(25, 'Custom asigra plugin image imported.')

        iocroot = self.middleware.call_sync('jail.get_iocroot')
        plugin_root_path = os.path.join(iocroot, 'jails', custom_jail_image, 'root')
        plugin_postgres_path = os.path.join(plugin_root_path, 'usr/local/pgsql/data')
        plugin_data_path = os.path.join(plugin_root_path, 'zdata/root')
        plugin_upgrade_path = os.path.join(plugin_root_path, 'zdata/Upgrade')
        plugin_tmp_path = os.path.join(plugin_root_path, 'zdata/tmp')

        system_data_path = os.path.join(system_asigra_path, 'files')
        system_tmp_path = os.path.join(system_asigra_path, 'tmp')

        # Plugin jail root dataset
        plugin_root_dataset = self.middleware.call_sync(
            'pool.dataset.query', [['mountpoint', '=', plugin_root_path]], {'get': True}
        )

        # Now we start the actual migration
        shutil.copy(
            os.path.join(plugin_data_path, 'Storage_Label.txt'), os.path.join(system_data_path, 'Storage_Label.txt')
        )

        shutil.rmtree(plugin_postgres_path)
        shutil.rmtree(plugin_data_path)
        shutil.rmtree(plugin_upgrade_path)
        shutil.rmtree(plugin_tmp_path)

        for dataset, new_path, new_mount in (
            (
                os.path.join(asigra_config['filesystem'], 'files'),
                os.path.join(plugin_root_dataset['id'], 'files'), plugin_data_path
            ),
            (
                os.path.join(asigra_config['filesystem'], 'database'),
                os.path.join(plugin_root_dataset['id'], 'database'), plugin_postgres_path
            ),
            (
                os.path.join(asigra_config['filesystem'], 'upgrade'),
                os.path.join(plugin_root_dataset['id'], 'upgrade'), plugin_upgrade_path
            ),
        ):
            self.middleware.call_sync('zfs.dataset.rename', dataset, {'new_name': new_path})

            # "new_mount" will start with "/mnt/something", we don't want "/mnt" to be included so we remove it
            self.middleware.call_sync(
                'zfs.dataset.update', new_path, {'properties': {'mountpoint': {'value': new_mount[4:]}}}
            )

        job.set_progress(80, 'Asigra datasets successfully migrated to asigra plugin.')

        with open(os.path.join(plugin_root_path, 'etc/passwd'), 'r') as f:
            ids = re.findall(r'pgsql.*:(\d+):(\d+):', f.read())
            if not ids:
                raise CallError(f'Postgres user could not be found in {custom_jail_image} jail.')
            else:
                uid, gid = ids[0]

        proc = subprocess.Popen(
            ['chown', '-R', f'{uid}:{gid}', plugin_postgres_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()

        if proc.returncode:
            raise CallError(f'Failed to set ownership of {plugin_postgres_path}: {stderr}')

        shutil.copytree(system_tmp_path, plugin_tmp_path)

        try:
            # Finally we destroy asigra parent dataset
            self.middleware.call_sync('pool.dataset.delete', asigra_dataset['id'])
        except Exception as e:
            self.middleware.logger.debug(f'Failed to destroy asigra dataset: {e}')

        self.middleware.call_sync('datastore.update', 'services.asigra', asigra_config['id'], {'filesystem': ''})
        self.middleware.logger.debug('Migration successfully performed.')

        job.set_progress(100, 'System asigra successfully migrated to asigra plugin')


def _event_system(middleware, event_type, args):
    if args['id'] != 'ready':
        return

    middleware.call_sync('asigra.migrate_to_plugin')


async def setup(middleware):
    middleware.event_subscribe('system', _event_system)
