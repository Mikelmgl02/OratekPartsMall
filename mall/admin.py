from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone
from .models import Account, Brand, BrandAlias, Invitation, InventoryUpdate, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
admin.site.site_header = "Administración de MotionPartes"
admin.site.site_title = "Administración de MotionPartes"
admin.site.index_title = "Gestión del catálogo"
admin.site.register(User, UserAdmin)
for model in [Account, Role, Membership, Invitation, PartCode]:
    admin.site.register(model)

class PartAdminForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = '__all__'
    def clean(self):
        data = super().clean()
        if data.get('estado') == 'rechazada' and not data.get('decision_reason'):
            self.add_error('decision_reason', 'Elige un motivo para rechazar la línea.')
        return data

@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    """estado is the line's lifecycle; active follows it (Part.save). Merging and Separar SKU own unificada."""
    form = PartAdminForm
    list_display = ['sku', 'name', 'estado', 'origen', 'active', 'merged_into']
    list_filter = ['estado', 'origen']
    search_fields = ['sku', 'name', 'description']
    readonly_fields = ['active', 'merged_into', 'decided_by', 'decided_at', 'version', 'created_at', 'updated_at', 'changed_at']
    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == 'estado':
            kwargs['choices'] = [choice for choice in db_field.choices if choice[0] != 'unificada']
        return super().formfield_for_choice_field(db_field, request, **kwargs)
    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + ['estado'] if obj is not None and obj.merged_into_id else fields
    def save_model(self, request, obj, form, change):
        if 'estado' in form.changed_data:  # a person decided it, as mall.lines.set_estado records
            obj.decided_by, obj.decided_at = request.user, timezone.now()
            obj.version, obj.changed_at = (obj.version or 0) + 1, obj.decided_at
        super().save_model(request, obj, form, change)

class BrandAliasForm(forms.ModelForm):
    class Meta:
        model = BrandAlias
        fields = '__all__'
    def clean_alias(self):
        alias = (self.cleaned_data.get('alias') or '').strip().upper()
        if Brand.objects.filter(nombre=' '.join(alias.split())).exists():
            raise forms.ValidationError('Ya es el nombre de una marca: para juntarlas usa mall.brands.merge.')
        return alias

class BrandAliasInline(admin.TabularInline):
    """Aliases are added here; one that exists is read only, as codes may carry its spelling (mall.brands.rename/merge move them)."""
    model = BrandAlias
    form = BrandAliasForm
    extra = 0
    can_delete = False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False

class BrandAdminForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = '__all__'
    def clean(self):
        data = super().clean()
        familia, brand = data.get('familia'), self.instance if self.instance.pk else None
        nombre = ' '.join((data.get('nombre') or '').upper().split())
        if brand is None and nombre and BrandAlias.objects.filter(alias=nombre).exists():
            self.add_error('nombre', 'Ya es un alias de otra marca.')
        if familia is not None:  # the rule of mall.brands.set_familia: one level
            if brand is not None and familia.pk == brand.pk:
                self.add_error('familia', 'Una marca no puede ser su propia familia.')
            elif familia.familia_id:
                self.add_error('familia', f'{familia.nombre} ya pertenece a la familia {familia.familia.nombre}: elige esa.')
            elif brand is not None and brand.miembros.exists():
                self.add_error('familia', f'{brand.nombre} encabeza una familia: no puede pertenecer a otra.')
        return data

@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    """The nombre of an existing brand changes only through mall.brands.rename or merge, which re-key the codes that carry it; a brand
    is merged into another, never deleted, so no code keeps a spelling no brand holds."""
    form = BrandAdminForm
    list_display = ['nombre', 'tipo', 'familia']
    list_filter = ['tipo']
    search_fields = ['nombre', 'aliases__alias']
    inlines = [BrandAliasInline]
    def get_readonly_fields(self, request, obj=None):
        return ['nombre', 'created_at', 'updated_at'] if obj is not None else ['created_at', 'updated_at']
    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(BrandAlias)
class BrandAliasAdmin(admin.ModelAdmin):
    form = BrandAliasForm
    list_display = ['alias', 'brand']
    search_fields = ['alias', 'brand__nombre']
    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)  # the list opens; an alias that exists is read only
    def has_delete_permission(self, request, obj=None):
        return False

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
