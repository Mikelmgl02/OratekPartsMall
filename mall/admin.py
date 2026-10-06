from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Account, Invitation, InventoryUpdate, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
admin.site.site_header = "Administración de MotionPartes"
admin.site.site_title = "Administración de MotionPartes"
admin.site.index_title = "Gestión del catálogo"
admin.site.register(User, UserAdmin)
for model in [Account, Role, Membership, Invitation, Part, PartCode]:
    admin.site.register(model)

@admin.register(SupplierItem)
class SupplierItemAdmin(admin.ModelAdmin):
    list_display = ['supplier', 'supplier_invent_id', 'codigo', 'part', 'matching_status']
    list_filter = ['matching_status', 'supplier']
    readonly_fields = ['supplier', 'supplier_invent_id', 'codigo', 'brand', 'description', 'source', 'reported_quantity', 'reserved_quantity', 'updated_at']
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(StockEntry, InventoryUpdate)
class LedgerAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
    def has_change_permission(self, request, obj=None):
        return False
