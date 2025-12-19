from .snmpd_helper import SnmpdHelper


def snmpd_helper():
    SnmpdHelper().run()

def ne_snmpd_helper():
    SnmpdHelper().ne_run()
