from freeadmin.tree import TreeNode
from django.utils.translation import ugettext_lazy as _

BLACKLIST = ['Email', 'Advanced', 'Settings', 'SSL']
NAME = _('System')
ICON = u'SystemIcon'

class Reporting(TreeNode):

        gname = 'Reporting'
        name = _(u'Reporting')
        view = 'system_reporting'
        icon = u"ReportingIcon"

class Info(TreeNode):

        gname = 'SysInfo'
        name = _(u'System Information')
        view = 'system_info'
        icon = u"InfoIcon"

class Settings(TreeNode):

        gname = 'Settings'
        name = _(u'Settings')
        view = 'system_settings'
        icon = u"SettingsIcon"

class ViewCron(TreeNode):

        gname = 'system.CronJob.View'
        name = _('View Cron Jobs')
        type = 'opencron'
        icon = u"ViewcronJobIcon"
        append_app = False

class ViewRsync(TreeNode):

        gname = 'system.Rsync.View'
        name = _('View Rsync Tasks')
        view = 'system_rsyncs'
        append_app = False

class ViewSmarttest(TreeNode):

        gname = 'system.SMARTTest.View'
        view = 'system_smarttests'
        name = _('View S.M.A.R.T. Tests')
        append_app = False

class ViewSysctl(TreeNode):

        gname = 'system.Sysctl.View'
        view = 'system_sysctls'
        name = _('View Sysctls')
        append_app = False

class ViewTunable(TreeNode):

        gname = 'system.Tunable.View'
        view = 'system_tunables'
        name = _('View Tunables')
        append_app = False

class ViewNTPServer(TreeNode):

        gname = 'system.NTPServer.View'
        view = 'system_ntpservers'
        #name = _('View NTP Servers')
        append_app = False
