def snmpd_helper():
    from .snmpd_helper import SnmpdHelper
    SnmpdHelper().run()

def ne_snmpd_helper():
    import sys
    from .snmpd_helper import SnmpdHelper
    helper = SnmpdHelper()
    if "--warm-cache" in sys.argv[1:]:
        helper.ne_warm_cache()
        return
    helper.ne_run()
