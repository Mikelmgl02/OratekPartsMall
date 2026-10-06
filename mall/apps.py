from django.apps import AppConfig


class MallConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mall"
    verbose_name = "MotionPartes"

    def ready(self):
        from django.db.models.signals import post_delete
        from .models import PartImage
        from .catalog_media import images_deleted
        post_delete.connect(images_deleted, sender=PartImage, dispatch_uid='catalog_image_cleanup')

        from django.db.models.signals import post_save
        from .models import Part, PartCode
        from .matching_queue import enqueue_matching
        for model in (Part, PartCode):
            post_save.connect(enqueue_matching, sender=model, dispatch_uid=f'matching_save_{model.__name__}')
            post_delete.connect(enqueue_matching, sender=model, dispatch_uid=f'matching_delete_{model.__name__}')
