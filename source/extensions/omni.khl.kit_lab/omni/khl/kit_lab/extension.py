import omni.ext
from omni.services.core import main

from .service import legacy_router, reset_namespace, router, stage_e


class Extension(omni.ext.IExt):
    def on_startup(self, ext_id):
        reset_namespace()
        generation = stage_e.start_bridge_generation()
        main.register_router(router)
        main.register_router(legacy_router)
        print("[omni.khl.kit_lab] Ready: http://127.0.0.1:8011/docs")
        print("[omni.khl.kit_lab] API: /khl/lab/* (legacy: /khl/ai/*)")
        print(f"[omni.khl.kit_lab] Stage E API ready; generation={generation}")

    def on_shutdown(self):
        main.deregister_router(legacy_router)
        main.deregister_router(router)
        reset_namespace()
        print("[omni.khl.kit_lab] Shutdown")
