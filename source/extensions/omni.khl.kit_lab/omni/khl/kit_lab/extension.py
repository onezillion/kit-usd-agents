import omni.ext
from omni.services.core import main

from .service import legacy_router, reset_namespace, router


class Extension(omni.ext.IExt):
    def on_startup(self, ext_id):
        reset_namespace()
        main.register_router(router)
        main.register_router(legacy_router)
        print("[omni.khl.kit_lab] Ready: http://127.0.0.1:8011/docs")
        print("[omni.khl.kit_lab] API: /khl/lab/* (legacy: /khl/ai/*)")
        print("[omni.khl.kit_lab] Deterministic read API: Phase 2B.1")

    def on_shutdown(self):
        main.deregister_router(legacy_router)
        main.deregister_router(router)
        reset_namespace()
        print("[omni.khl.kit_lab] Shutdown")
