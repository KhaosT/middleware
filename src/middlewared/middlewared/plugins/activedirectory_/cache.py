import os
import shutil
import subprocess
import tdb

from middlewared.plugins.smb import SMBCmd, SMBPath
from middlewared.service import Service, private, job
from middlewared.service_exception import CallError


class ActiveDirectoryService(Service):
    class Config:
        service = "activedirectory"

    @private
    def get_gencache_sid(self, tdb_key):
        gencache = tdb.Tdb('/tmp/gencache.tdb', 0, tdb.DEFAULT, os.O_RDONLY)
        try:
            tdb_val = gencache.get(tdb_key)
        finally:
            gencache.close()

        if tdb_val is None:
            return None

        decoded_sid = tdb_val[8:-5].decode()
        if decoded_sid == '-':
            return None

        return decoded_sid

    @private
    def get_gencache_names(self, idmap_domain):
        out = []
        known_doms = [x['domain_info']['name'] for x in idmap_domain]

        gencache = tdb.Tdb('/tmp/gencache.tdb', 0, tdb.DEFAULT, os.O_RDONLY)
        try:
            for k in gencache.keys():
                if k[:8] != b'NAME2SID':
                    continue
                key = k[:-1].decode()
                name = key.split('/', 1)[1]
                dom = name.split('\\')[0]
                if dom not in known_doms:
                    continue

                out.append(name)
        finally:
            gencache.close()

        return out

    @private
    def get_entries(self, data):
        ret = []
        entry_type = data.get('entry_type')
        do_wbinfo = data.get('cache_enabled', True)

        shutil.copyfile(f'{SMBPath.LOCKDIR.platform()}/gencache.tdb', '/tmp/gencache.tdb')

        domain_info = self.middleware.call_sync(
            'idmap.query', [], {'extra': {'additional_information': ['DOMAIN_INFO']}}
        )
        for dom in domain_info.copy():
            if not dom['domain_info']:
                domain_info.remove(dom)

        dom_by_sid = {x['domain_info']['sid']: x for x in domain_info}

        if do_wbinfo:
            wb = subprocess.run(
                [SMBCmd.WBINFO.value, f'-{entry_type[0].lower()}'], capture_output=True
            )
            if wb.returncode != 0:
                raise CallError(f'Failed to retrieve {entry_type} from active directory: '
                                f'{wb.stderr.decode().strip()}')
            entries = wb.stdout.decode().splitlines()
            nss_look_job = self.middleware.call_sync(
                'idmap.nss_lookup',
                {'record_type': entry_type, 'principals': entries},
                0,
            )
            nss_entries = nss_look_job.wait_sync(raise_error=True)

        else:
            entries = self.get_gencache_names(domain_info)
            nss_entries = {}
            for i in entries:
                try:
                    res = self.middleware.call_sync(
                       f'{entry_type.lower()}.get_{entry_type.lower()}_obj',
                       {f'{entry_type.lower()}name': i}
                    )
                except CallError:
                    continue

                nss_entries.update(res)

        for i in entries:
            entry = {"id": -1, "sid": None, "nss": None}
            try:
                entry["nss"] = nss_entries[i]
            except KeyError:
                continue
            if entry_type == 'USER':
                entry["id"] = entry["nss"]['pw_uid']
                tdb_key = f'IDMAP/UID2SID/{entry["id"]}'

            else:
                entry["id"] = entry["nss"]['gr_gid']
                tdb_key = f'IDMAP/GID2SID/{entry["id"]}'

            """
            Try to look up in gencache before subprocess to wbinfo.
            """
            entry['sid'] = self.get_gencache_sid((tdb_key.encode() + b"\x00"))
            if not entry['sid']:
                entry['sid'] = self.middleware.call_sync('idmap.unixid_to_sid', {
                    'id_type': entry_type,
                    'id': entry['id'],
                })

            entry['domain_info'] = dom_by_sid[entry['sid'].rsplit('-', 1)[0]]
            ret.append(entry)

        return ret

    @private
    @job(lock='fill_ad_cache')
    def fill_cache(self, job, force=False):
        ad = self.middleware.call_sync('activedirectory.config')
        id_type_both_backends = [
            'RID',
            'AUTORID'
        ]

        users = self.get_entries({'entry_type': 'USER', 'cache_enabled': not ad['disable_freenas_cache']})
        for u in users:
            user_data = u['nss']
            rid = int(u['sid'].rsplit('-', 1)[1])

            entry = {
                'id': 100000 + u['domain_info']['range_low'] + rid,
                'uid': user_data['pw_uid'],
                'username': user_data['pw_name'],
                'unixhash': None,
                'smbhash': None,
                'group': {},
                'home': '',
                'shell': '',
                'full_name': user_data['pw_gecos'],
                'builtin': False,
                'email': '',
                'password_disabled': False,
                'locked': False,
                'sudo': False,
                'sudo_nopasswd': False,
                'sudo_commands': [],
                'microsoft_account': False,
                'attributes': {},
                'groups': [],
                'sshpubkey': None,
                'local': False,
                'id_type_both': u['domain_info']['idmap_backend'] in id_type_both_backends,
                'nt_name': None,
                'sid': None,
            }
            self.middleware.call_sync('dscache.insert', self._config.namespace.upper(), 'USER', entry)

        groups = self.get_entries({'entry_type': 'GROUP', 'cache_enabled': not ad['disable_freenas_cache']})
        for g in groups:
            group_data = g['nss']
            rid = int(g['sid'].rsplit('-', 1)[1])

            entry = {
                'id': 100000 + g['domain_info']['range_low'] + rid,
                'gid': group_data['gr_gid'],
                'name': group_data['gr_name'],
                'group': group_data['gr_name'],
                'builtin': False,
                'sudo': False,
                'sudo_nopasswd': False,
                'sudo_commands': [],
                'users': [],
                'local': False,
                'id_type_both': g['domain_info']['idmap_backend'] in id_type_both_backends,
                'nt_name': None,
                'sid': None,
            }
            self.middleware.call_sync('dscache.insert', self._config.namespace.upper(), 'GROUP', entry)

    @private
    async def get_cache(self):
        users = await self.middleware.call('dscache.entries', self._config.namespace.upper(), 'USER')
        groups = await self.middleware.call('dscache.entries', self._config.namespace.upper(), 'GROUP')
        return {"USERS": users, "GROUPS": groups}
