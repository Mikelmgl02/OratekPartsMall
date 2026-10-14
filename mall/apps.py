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
        from .models import Part, Reference
        from .matching_queue import enqueue_matching
        # The uids keep their pre-S2a names (PartCode), so a reload never connects a handler twice.
        for model, name in ((Part, 'Part'), (Reference, 'PartCode')):
            post_save.connect(enqueue_matching, sender=model, dispatch_uid=f'matching_save_{name}')
            post_delete.connect(enqueue_matching, sender=model, dispatch_uid=f'matching_delete_{name}')

        # The OEM reference library mirrors every OEM alterno (a Reference of tipo oem with a brand) until S4 makes the References the
        # library; the handlers never break the Reference write. A retire drops the source the alterno backed, never the number.
        from django.db.models.signals import pre_save
        from .oem_reference import partcode_deleted, partcode_pre_save, partcode_saved
        pre_save.connect(partcode_pre_save, sender=Reference, dispatch_uid='oem_reference_pre_save')
        post_save.connect(partcode_saved, sender=Reference, dispatch_uid='oem_reference_save')
        post_delete.connect(partcode_deleted, sender=Reference, dispatch_uid='oem_reference_delete')

        # A renamed, created or grouped SKU refreshes the link to the OEM number its own code names; never breaks the Part write.
        from .oem_links import part_pre_save, part_saved
        pre_save.connect(part_pre_save, sender=Part, dispatch_uid='oem_links_pre_save')
        post_save.connect(part_saved, sender=Part, dispatch_uid='oem_links_save')

        # A message or a deal event notifies the other party: a browser push (off without VAPID keys) and an email (off without Resend).
        from .models import DealEvent, DealMessage
        from .push import event_saved, message_saved
        post_save.connect(message_saved, sender=DealMessage, dispatch_uid='push_deal_message')
        post_save.connect(event_saved, sender=DealEvent, dispatch_uid='push_deal_event')
        from . import email_notifications
        post_save.connect(email_notifications.message_saved, sender=DealMessage, dispatch_uid='email_deal_message')
        post_save.connect(email_notifications.event_saved, sender=DealEvent, dispatch_uid='email_deal_event')
        # ... and nudges the open pages of both parties over the websocket (off without REDIS_URL).
        from . import realtime
        post_save.connect(realtime.message_saved, sender=DealMessage, dispatch_uid='realtime_deal_message')
        post_save.connect(realtime.event_saved, sender=DealEvent, dispatch_uid='realtime_deal_event')
        # ... and a WhatsApp notice to those who opted in (off without the WhatsApp Cloud API settings).
        from . import whatsapp
        post_save.connect(whatsapp.message_saved, sender=DealMessage, dispatch_uid='whatsapp_deal_message')
        post_save.connect(whatsapp.event_saved, sender=DealEvent, dispatch_uid='whatsapp_deal_event')
