from django.contrib import admin
from .models import (
    Source, Document, ExtractionRun,
    Entity, EntityIdentifier, EntityAlias, EntityMerge,
    AttributeDef, PredicateDef, Assertion, Conflict, Relation,
    Taxonomy, TaxonomyNode, Classification,
    LLMCall, ScheduledSource,
)


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'domain', 'base_trust']
    list_filter = ['kind']
    search_fields = ['name', 'domain']


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'source', 'lang', 'published_at', 'fetched_at']
    list_filter = ['source', 'lang', 'media_type']
    search_fields = ['title', 'url']
    raw_id_fields = ['source']
    readonly_fields = ['fetched_at', 'content_sha256']


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = ['task', 'model', 'code_version', 'status', 'started_at', 'finished_at']
    list_filter = ['task', 'status', 'model']
    readonly_fields = ['started_at']


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ['canonical_name', 'entity_type', 'status', 'created_at']
    list_filter = ['entity_type', 'status']
    search_fields = ['canonical_name', 'slug']
    prepopulated_fields = {'slug': ('canonical_name',)}
    readonly_fields = ['created_at']
    raw_id_fields = ['redirects_to']


@admin.register(EntityIdentifier)
class EntityIdentifierAdmin(admin.ModelAdmin):
    list_display = ['entity', 'scheme', 'value', 'confidence']
    list_filter = ['scheme']
    search_fields = ['value']
    raw_id_fields = ['entity', 'document']


@admin.register(EntityAlias)
class EntityAliasAdmin(admin.ModelAdmin):
    list_display = ['alias', 'alias_kind', 'entity', 'lang']
    list_filter = ['alias_kind', 'lang']
    search_fields = ['alias', 'alias_norm']
    raw_id_fields = ['entity', 'document']


@admin.register(EntityMerge)
class EntityMergeAdmin(admin.ModelAdmin):
    list_display = ['merged', 'kept', 'performed_by', 'merged_at', 'reverted_at']
    raw_id_fields = ['kept', 'merged']
    readonly_fields = ['merged_at']

    actions = ['revert_merge']

    @admin.action(description='Revert selected merges')
    def revert_merge(self, request, queryset):
        from django.utils import timezone
        queryset.filter(reverted_at__isnull=True).update(reverted_at=timezone.now())


@admin.register(AttributeDef)
class AttributeDefAdmin(admin.ModelAdmin):
    list_display = ['key', 'entity_type', 'datatype', 'cardinality', 'volatility_days', 'is_projected', 'is_promoted']
    list_filter = ['entity_type', 'datatype', 'cardinality', 'is_projected']
    search_fields = ['key', 'label']


@admin.register(PredicateDef)
class PredicateDefAdmin(admin.ModelAdmin):
    list_display = ['key', 'label']
    search_fields = ['key', 'label']


@admin.register(Assertion)
class AssertionAdmin(admin.ModelAdmin):
    list_display = ['entity', 'attribute', 'method', 'confidence', 'status', 'review_state', 'observed_at']
    list_filter = ['method', 'status', 'review_state']
    search_fields = ['entity__canonical_name', 'quote']
    raw_id_fields = ['entity', 'document', 'run', 'value_entity']
    readonly_fields = ['observed_at', 'created_at']

    actions = ['accept', 'reject', 'flag_for_review']

    @admin.action(description='Accept selected assertions')
    def accept(self, request, queryset):
        queryset.update(status='accepted', review_state='approved')

    @admin.action(description='Reject selected assertions')
    def reject(self, request, queryset):
        queryset.update(status='rejected')

    @admin.action(description='Flag for review')
    def flag_for_review(self, request, queryset):
        queryset.update(review_state='pending')


@admin.register(Conflict)
class ConflictAdmin(admin.ModelAdmin):
    list_display = ['entity', 'attribute_key', 'severity', 'resolution', 'detected_at', 'resolved_at']
    list_filter = ['severity', 'resolution']
    search_fields = ['entity__canonical_name', 'attribute_key']
    raw_id_fields = ['entity']
    readonly_fields = ['detected_at', 'assertion_ids']

    actions = ['mark_pending']

    @admin.action(description='Mark as pending review')
    def mark_pending(self, request, queryset):
        queryset.update(resolution='pending')


@admin.register(Relation)
class RelationAdmin(admin.ModelAdmin):
    list_display = ['subject', 'predicate', 'object', 'method', 'confidence', 'status']
    list_filter = ['predicate', 'method', 'status']
    raw_id_fields = ['subject', 'object', 'document', 'run']


@admin.register(Taxonomy)
class TaxonomyAdmin(admin.ModelAdmin):
    list_display = ['key', 'version', 'status']
    list_filter = ['status']


@admin.register(TaxonomyNode)
class TaxonomyNodeAdmin(admin.ModelAdmin):
    list_display = ['path', 'label', 'taxonomy']
    list_filter = ['taxonomy']
    search_fields = ['path', 'label', 'definition']


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ['entity', 'node', 'weight', 'is_primary', 'confidence', 'method']
    list_filter = ['is_primary', 'method']
    raw_id_fields = ['entity', 'node', 'document', 'run']


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    list_display = ['task', 'model', 'tokens_in', 'tokens_out', 'cost_usd', 'called_at']
    list_filter = ['task', 'model']
    readonly_fields = ['called_at', 'cost_usd', 'tokens_in', 'tokens_out']
    raw_id_fields = ['run', 'entity']
    date_hierarchy = 'called_at'


@admin.register(ScheduledSource)
class ScheduledSourceAdmin(admin.ModelAdmin):
    list_display = ['source', 'feed_type', 'cadence', 'is_active', 'last_checked_at']
    list_filter = ['feed_type', 'cadence', 'is_active']
    search_fields = ['feed_url', 'source__name']
    raw_id_fields = ['source', 'entity_hint']

    actions = ['activate', 'deactivate']

    @admin.action(description='Activate selected sources')
    def activate(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description='Deactivate selected sources')
    def deactivate(self, request, queryset):
        queryset.update(is_active=False)
