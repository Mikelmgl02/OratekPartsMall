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

        # From inventory refactor S4 the OEM library's numbers are References (mall.references.add_library_number): the signals that
        # mirrored every OEM alterno into the OEM table and refreshed a SKU's name links are gone with it.

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
