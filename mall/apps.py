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

        # The OEM reference library mirrors every OEM alterno; the handlers never break the PartCode write.
        from django.db.models.signals import pre_save
        from .oem_reference import partcode_deleted, partcode_pre_save, partcode_saved
        pre_save.connect(partcode_pre_save, sender=PartCode, dispatch_uid='oem_reference_pre_save')
        post_save.connect(partcode_saved, sender=PartCode, dispatch_uid='oem_reference_save')
        post_delete.connect(partcode_deleted, sender=PartCode, dispatch_uid='oem_reference_delete')

        # A renamed, created or grouped SKU refreshes the link to the OEM number its own code names; never breaks the Part write.
        from .oem_links import part_pre_save, part_saved
        pre_save.connect(part_pre_save, sender=Part, dispatch_uid='oem_links_pre_save')
        post_save.connect(part_saved, sender=Part, dispatch_uid='oem_links_save')
