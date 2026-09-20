"""Seed all AI system prompts into the PromptTemplate table.

Imports the hardcoded strings directly from the task modules so the seed
stays in sync with the code. Safe to re-run — uses get_or_create on
(key, version=1) and only activates if no active version already exists.

Usage:
    python manage.py seed_prompts
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Seed AI prompt templates into the DB from hardcoded task module strings.'

    def handle(self, *args, **options):
        from core.models import PromptTemplate

        # Import prompts from task modules.
        # Each import pulls the module-level constant directly.
        from ingest.tasks.triage import _SYSTEM as TRIAGE
        from ingest.tasks.extract import _SYSTEM as EXTRACT_CLAIMS
        from ingest.tasks.relate import _SYSTEM as RELATE_DOC, _SYSTEM_SONAR as RELATE_SONAR
        from ingest.tasks.resolve import _LLM_RESOLVE_SYSTEM as RESOLVE
        from ingest.tasks.synthesize import _SYSTEM as SYNTHESIZE
        from ingest.tasks.summarise import _SYSTEM as SUMMARISE
        from ingest.tasks.evolve import _SYSTEM as EVOLVE_TAXONOMY
        from ingest.tasks.research import _SYSTEM as RESEARCH_MAIN, _ANGLES_SYSTEM as RESEARCH_ANGLES
        from ingest.tasks.classify import (
            _SYSTEM_COMPANY as CLASSIFY_COMPANY,
            _SYSTEM_FUNDING_PROGRAM as CLASSIFY_FUNDING_PROGRAM,
            _SYSTEM_PERSON as CLASSIFY_PERSON,
            _SYSTEM_PROGRAM as CLASSIFY_PROGRAM,
            _SYSTEM_END_USER as CLASSIFY_END_USER,
        )

        prompts = [
            dict(
                key='triage',
                label='Relevance Gate',
                description='Classifies whether a document contains extractable space-industry facts.',
                system_prompt=TRIAGE,
            ),
            dict(
                key='research_main',
                label='Main Research Extractor',
                description='Powers all Sonar web research calls. Defines the 4-section output: claims, events, fragments, relations.',
                system_prompt=RESEARCH_MAIN,
            ),
            dict(
                key='research_angles',
                label='Search Angle Generator',
                description='Generates 2 targeted search queries for each research topic.',
                system_prompt=RESEARCH_ANGLES,
            ),
            dict(
                key='extract_claims',
                label='Document Claim Extractor',
                description='Extracts structured key-value claims from raw documents (non-Sonar path). Requires verbatim quotes.',
                system_prompt=EXTRACT_CLAIMS,
            ),
            dict(
                key='relate_document',
                label='Relation Extractor (document)',
                description='Extracts typed graph edges from raw documents. Verbatim quote required.',
                system_prompt=RELATE_DOC,
            ),
            dict(
                key='relate_sonar',
                label='Relation Extractor (Sonar)',
                description='Extracts relations from Sonar research text. Approximate quotes accepted.',
                system_prompt=RELATE_SONAR,
            ),
            dict(
                key='resolve_entity',
                label='Entity Resolver',
                description='Shared system prompt for L4 (disambiguation) and L5 (canonical name lookup).',
                system_prompt=RESOLVE,
            ),
            dict(
                key='synthesize_conflict',
                label='Conflict Synthesiser',
                description='Resolves conflicting assertions about the same (entity, attribute) pair.',
                system_prompt=SYNTHESIZE,
            ),
            dict(
                key='summarise_entity',
                label='Entity Summary',
                description='Synthesises a structured prose portrait from an entity\'s events and fragments.',
                system_prompt=SUMMARISE,
            ),
            dict(
                key='evolve_taxonomy',
                label='Taxonomy Evolution',
                description='Proposes new taxonomy nodes or alters existing ones based on recently ingested entities.',
                system_prompt=EVOLVE_TAXONOMY,
            ),
            dict(
                key='classify_company',
                label='Classifier — Company / Entity',
                description='Classifies companies and entities across value_chain, orbit_regime, customer_type, maturity, research_area, adjacent_sector.',
                system_prompt=CLASSIFY_COMPANY,
            ),
            dict(
                key='classify_funding_program',
                label='Classifier — Funding Programme',
                description='Classifies funding instruments under the funding_type facet.',
                system_prompt=CLASSIFY_FUNDING_PROGRAM,
            ),
            dict(
                key='classify_person',
                label='Classifier — Person',
                description='Classifies people under the research_area facet.',
                system_prompt=CLASSIFY_PERSON,
            ),
            dict(
                key='classify_program',
                label='Classifier — Space Programme',
                description='Classifies space programmes under value_chain and orbit_regime.',
                system_prompt=CLASSIFY_PROGRAM,
            ),
            dict(
                key='classify_end_user',
                label='Classifier — End User',
                description='Classifies end users across adjacent_sector, customer_type, and downstream value_chain.',
                system_prompt=CLASSIFY_END_USER,
            ),
        ]

        created = activated = skipped = 0
        for p in prompts:
            key = p['key']
            pt, is_new = PromptTemplate.objects.get_or_create(
                key=key,
                version=1,
                defaults={
                    'label': p['label'],
                    'description': p['description'],
                    'system_prompt': p['system_prompt'],
                    'is_active': False,
                    'notes': 'Initial seed from hardcoded task module string.',
                },
            )
            if is_new:
                created += 1

            # Activate v1 only if nothing is currently active for this key
            if not PromptTemplate.objects.filter(key=key, is_active=True).exists():
                pt.activate()
                activated += 1
                self.stdout.write(f'  activated: {key} v1')
            else:
                skipped += 1
                self.stdout.write(f'  skipped (already active): {key}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. {created} created, {activated} activated, {skipped} already active.'
        ))
