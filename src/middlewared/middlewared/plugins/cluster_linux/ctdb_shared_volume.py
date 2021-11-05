import errno
from pathlib import Path
from glustercli.cli import volume

from middlewared.service import Service, CallError, job
from middlewared.plugins.cluster_linux.utils import CTDBConfig
from middlewared.validators import IpAddress


MOUNT_UMOUNT_LOCK = CTDBConfig.MOUNT_UMOUNT_LOCK.value
CRE_OR_DEL_LOCK = CTDBConfig.CRE_OR_DEL_LOCK.value
CTDB_VOL_NAME = CTDBConfig.CTDB_VOL_NAME.value
CTDB_LOCAL_MOUNT = CTDBConfig.CTDB_LOCAL_MOUNT.value


class CtdbSharedVolumeService(Service):

    class Config:
        namespace = 'ctdb.shared.volume'
        private = True

    async def validate(self):
        filters = [('id', '=', CTDB_VOL_NAME)]
        ctdb = await self.middleware.call('gluster.volume.query', filters)
        if not ctdb:
            # it's expected that ctdb shared volume exists when
            # calling this method
            raise CallError(f'{CTDB_VOL_NAME} does not exist', errno.ENOENT)

        for i in ctdb:
            err_msg = f'A volume named "{CTDB_VOL_NAME}" already exists '
            if i['type'] != 'REPLICATE':
                err_msg += (
                    'but is not a "REPLICATE" type volume. '
                    'Please delete or rename this volume and try again.'
                )
                raise CallError(err_msg)
            elif i['replica'] < 3 or i['num_bricks'] < 3:
                err_msg += (
                    'but is configured in a way that '
                    'could cause data corruption. Please delete '
                    'or rename this volume and try again.'
                )
                raise CallError(err_msg)

    @job(lock=CRE_OR_DEL_LOCK)
    async def create(self, job):
        """
        Create and mount the shared volume to be used
        by ctdb daemon.
        """
        # get the peers in the TSP
        peers = await self.middleware.call('gluster.peer.query')
        if not peers:
            raise CallError('No peers detected')

        # shared storage volume requires 3 nodes, minimally, to
        # prevent the dreaded split-brain
        con_peers = [i['hostname'] for i in peers if i['connected'] == 'Connected']
        if len(con_peers) < 3:
            raise CallError(
                '3 peers must be present and connected before the ctdb '
                'shared volume can be created.'
            )

        # check if ctdb shared volume already exists and started
        info = await self.middleware.call('gluster.volume.exists_and_started', CTDB_VOL_NAME)
        if not info['exists']:
            # get the system dataset location
            ctdb_sysds_path = (await self.middleware.call('systemdataset.config'))['path']
            ctdb_sysds_path = str(Path(ctdb_sysds_path).joinpath(CTDB_VOL_NAME))

            bricks = []
            for i in con_peers:
                bricks.append(i + ':' + ctdb_sysds_path)

            options = {'args': (CTDB_VOL_NAME, bricks,)}
            options['kwargs'] = {'replica': len(con_peers), 'force': True}
            await self.middleware.call('gluster.method.run', volume.create, options)

        # make sure the shared volume is configured properly to prevent
        # possibility of split-brain/data corruption with ctdb service
        await self.middleware.call('ctdb.shared.volume.validate')

        if not info['started']:
            # start it if we get here
            await self.middleware.call('gluster.volume.start', {'name': CTDB_VOL_NAME})

        # try to mount it locally and send a request
        # to all the other peers in the TSP to also
        # FUSE mount it
        data = {'event': 'VOLUME_START', 'name': CTDB_VOL_NAME, 'forward': True}
        await self.middleware.call('gluster.localevents.send', data)

        # we need to wait on the local FUSE mount job since
        # ctdb daemon config is dependent on it being mounted
        fuse_mount_job = await self.middleware.call('core.get_jobs', [
            ('method', '=', 'gluster.fuse.mount'),
            ('arguments.0.name', '=', 'ctdb_shared_vol'),
            ('state', '=', 'RUNNING')
        ])
        if fuse_mount_job:
            wait_id = await self.middleware.call('core.job_wait', fuse_mount_job[0]['id'])
            await wait_id.wait()

        # The peers in the TSP could be using dns names while ctdb
        # only accepts IP addresses. This means we need to resolve
        # the hostnames of the peers in the TSP to their respective
        # IP addresses so we can write them to the ctdb private ip file.
        names = []
        ips = []
        for peer in con_peers:
            # gluster.peer.query will return hostnames for the peers
            # if DNS resolves them, else it will return whatever was
            # given to us by end-user (IPs or DNS names)
            try:
                IpAddress()(peer)
                ips.append(peer)
            except ValueError:
                # means this is more than likely a hostname
                # so add it to list for DNS resolution
                names.append(peer)

        if names:
            try:
                ips.extend([i['address'] for i in await self.middleware.call('dnsclient.forward_lookup', names)])
            except Exception as e:
                raise CallError(f'Failed to resolve gluster peers hostnames: {e}')

        if len(ips) != len(con_peers):
            # means we had to resolve hostnames for at least one of the gluster peers
            # and the DNS resolution returned multiple IP addresses for that peer.
            # No easy way to handle this so raise an error.
            raise CallError('Gluster peer DNS name(s) resolved to more than 1 IP address')

        # Setup the ctdb daemon config. Without ctdb daemon running, none of the
        # sharing services (smb/nfs) will work in an active-active setting.
        priv_ctdb_ips = [i['address'] for i in await self.middleware.call('ctdb.private.ips.query')]
        for ip_to_add in [i for i in ips if i not in [j for j in priv_ctdb_ips]]:
            ip_add_job = await self.middleware.call('ctdb.private.ips.create', {'ip': ip_to_add})
            await ip_add_job.wait()

        # this sends an event telling all peers in the TSP (including this system)
        # to start the ctdb service
        data = {'event': 'CTDB_START', 'name': CTDB_VOL_NAME, 'forward': True}
        await self.middleware.call('gluster.localevents.send', data)

        return await self.middleware.call('gluster.volume.query', [('name', '=', CTDB_VOL_NAME)])

    @job(lock=CRE_OR_DEL_LOCK)
    async def delete(self, job):
        """
        Delete and unmount the shared volume used by ctdb daemon.
        """

        # nothing to delete if it doesn't exist
        info = await self.middleware.call('gluster.volume.exists_and_started', CTDB_VOL_NAME)
        if not info['exists']:
            return

        data = {'event': None, 'name': CTDB_VOL_NAME, 'forward': True}

        # need to stop smb locally and on all peers
        if await self.middleware.call('service.started', 'cifs'):
            data['event'] = 'SMB_STOP'
            await self.middleware.call('gluster.localevents.send', data)

        # need to stop ctdb locally and on all peers
        data['event'] = 'CTDB_STOP'
        await self.middleware.call('gluster.localevents.send', data)

        # need to unmount the gluster fuse mountpoint locally and on all peers
        data['event'] = 'VOLUME_STOP'
        await self.middleware.call('gluster.localevents.send', data)

        # stop the gluster volume
        if info['started']:
            options = {'args': (CTDB_VOL_NAME,), 'kwargs': {'force': True}}
            await self.middleware.call('gluster.method.run', volume.stop, options)

        # finally, we delete it
        await self.middleware.call('gluster.method.run', volume.delete, {'args': (CTDB_VOL_NAME,)})
