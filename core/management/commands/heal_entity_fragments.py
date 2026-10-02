"""Heal resolution debris: fold EMPTY fragment entities into their established container.

The failure shape this fixes (diagnosed on live data — "Spire"):

    An early unresolved mention created a data-less entity that OWNS the alias
    ('spire'), so every later mention of "Spire" resolves to the husk instead of
    "Spire Global" — a permanent false split, self-reinforcing as research
    accumulates on the wrong node.

A FRAGMENT is an entity with no accepted assertions, no events, and no knowledge
fragments, whose normalised name is whole-word contained in (or contains) an
ESTABLISHED entity's name (>= 3 accepted assertions), same type-group.

The merge is deterministic — no LLM — because there is no evidence to
misattribute: the fragment's name becomes an alias of the container (so future
mentions route correctly), the husk is marked merged, and an EntityMerge audit
row is written. Genuinely distinct subsidiaries (ICEYE US, SATLANTIS US) have
evidence and are untouched.

Preview by default; --apply writes.

Usage:
    python manage.py heal_entity_fragments            # report
    python manage.py heal_entity_fragments --apply
"""
from django.core.management.base import BaseCommand
from django.db import connection

from core.models import Entity

_EMPTY_EXCLUDED_TYPES = ('geography', 'document_node', 'event')


class Command(BaseCommand):
    help = 'Merge empty name-fragment entities into their established container.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Perform the merges (default: report only).')
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--no-llm', action='store_true',
                            help='Skip LLM arbitration (deterministic prefix rule only).')

    def handle(self, *args, **options):
        from ingest.tasks.resolve import _GEOGRAPHIC_BLOCKLIST

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT frag.id::text, frag.canonical_name, frag.entity_type,
                       fa.alias_norm, ca.alias_norm,
                       cont.id::text, cont.canonical_name,
                       (SELECT count(*) FROM assertion aa
                         WHERE aa.entity_id = cont.id
                           AND aa.status = 'accepted'
                           AND aa.superseded_at IS NULL) AS cont_n
                FROM entity frag
                JOIN entity_alias fa ON fa.entity_id = frag.id
                JOIN entity_alias ca ON ca.alias_norm LIKE fa.alias_norm || ' %%'
                JOIN entity cont ON cont.id = ca.entity_id AND cont.status = 'active'
                WHERE frag.status IN ('active', 'stub')
                  AND fa.alias_norm <> ca.alias_norm
                  AND char_length(fa.alias_norm) >= 3
                  AND frag.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND cont.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND NOT EXISTS (SELECT 1 FROM assertion x
                                   WHERE x.entity_id = frag.id
                                     AND x.status = 'accepted'
                                     AND x.superseded_at IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM entity_event x WHERE x.entity_id = frag.id)
                  AND NOT EXISTS (SELECT 1 FROM knowledge_fragment x WHERE x.entity_id = frag.id)
                ORDER BY cont_n DESC
                """
            )
            rows = cur.fetchall()

            # Second pass: husks with NO aliases at all (their name was already
            # rebound away) — match the canonical name as a prefix of any
            # established entity's alias instead.
            cur.execute(
                """
                SELECT frag.id::text, frag.canonical_name, frag.entity_type,
                       lower(frag.canonical_name) AS fa_norm, ca.alias_norm,
                       cont.id::text, cont.canonical_name,
                       (SELECT count(*) FROM assertion aa
                         WHERE aa.entity_id = cont.id
                           AND aa.status = 'accepted'
                           AND aa.superseded_at IS NULL) AS cont_n
                FROM entity frag
                JOIN entity_alias ca ON ca.alias_norm LIKE lower(frag.canonical_name) || ' %%'
                JOIN entity cont ON cont.id = ca.entity_id AND cont.status = 'active'
                WHERE frag.status IN ('active', 'stub')
                  AND char_length(lower(frag.canonical_name)) >= 3
                  AND frag.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND cont.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND NOT EXISTS (SELECT 1 FROM entity_alias fa WHERE fa.entity_id = frag.id)
                  AND NOT EXISTS (SELECT 1 FROM assertion x
                                   WHERE x.entity_id = frag.id
                                     AND x.status = 'accepted'
                                     AND x.superseded_at IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM entity_event x WHERE x.entity_id = frag.id)
                  AND NOT EXISTS (SELECT 1 FROM knowledge_fragment x WHERE x.entity_id = frag.id)
                ORDER BY cont_n DESC
                """
            )
            rows.extend(cur.fetchall())

            # Second pass: husks with NO aliases at all (their name was already
            # rebound away) — match the canonical name as a prefix of any
            # established entity's alias instead.
            cur.execute(
                """
                SELECT frag.id::text, frag.canonical_name, frag.entity_type,
                       lower(frag.canonical_name) AS fa_norm, ca.alias_norm,
                       cont.id::text, cont.canonical_name,
                       (SELECT count(*) FROM assertion aa
                         WHERE aa.entity_id = cont.id
                           AND aa.status = 'accepted'
                           AND aa.superseded_at IS NULL) AS cont_n
                FROM entity frag
                JOIN entity_alias ca ON ca.alias_norm LIKE lower(frag.canonical_name) || ' %%'
                JOIN entity cont ON cont.id = ca.entity_id AND cont.status = 'active'
                WHERE frag.status IN ('active', 'stub')
                  AND char_length(lower(frag.canonical_name)) >= 3
                  AND frag.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND cont.entity_type NOT IN ('geography', 'document_node', 'event')
                  AND NOT EXISTS (SELECT 1 FROM entity_alias fa WHERE fa.entity_id = frag.id)
                  AND NOT EXISTS (SELECT 1 FROM assertion x
                                   WHERE x.entity_id = frag.id
                                     AND x.status = 'accepted'
                                     AND x.superseded_at IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM entity_event x WHERE x.entity_id = frag.id)
                  AND NOT EXISTS (SELECT 1 FROM knowledge_fragment x WHERE x.entity_id = frag.id)
                ORDER BY cont_n DESC
                """
            )
            rows.extend(cur.fetchall())

        # ── One-directional truncation rule ──────────────────────────────
        # Debris = the fragment is a TRUNCATION of the container ("Spire" of
        # "Spire Global"). A fragment that ADDS specificity to the container
        # ("Airbus DS UK" over "Airbus", "ESA ESTEC" over "ESA") is a genuine
        # subsidiary — the blueprint says model those separately. Only
        # container_alias STARTING WITH fragment_alias + space qualifies.
        best: dict[str, tuple] = {}
        for frag_id, frag_name, frag_type, fa_norm, ca_norm, cont_id, cont_name, cont_n in rows:
            if cont_n < 3:
                continue
            if not ca_norm.startswith(fa_norm + ' '):
                continue
            if frag_name.strip().lower() in _GEOGRAPHIC_BLOCKLIST:
                continue
            if frag_id not in best or cont_n > best[frag_id][3]:
                best[frag_id] = (frag_id, frag_name, frag_type, cont_n, cont_id, cont_name)

        from ingest.tasks.dedup import _TYPE_TO_GROUP

        candidates = []
        for frag_id, (fid, fname, ftype, cont_n, cid, cname) in best.items():
            cont = Entity.objects.filter(pk=cid).only('entity_type').first()
            if not cont:
                continue
            if _TYPE_TO_GROUP.get(ftype) != _TYPE_TO_GROUP.get(cont.entity_type):
                continue  # a person fragment is not folded into a company
            candidates.append((fid, fname, cid, cname, cont_n))

        candidates.sort(key=lambda c: -c[4])
        candidates = candidates[:options['limit']]

        if not candidates:
            self.stdout.write('No empty fragments contained in established entities. Nothing to heal.')
            return

        # ── LLM arbitration: truncation-debris vs parent-vs-subsidiary ────
        # "Spire" of "Spire Global" is the same org; "Amazon" of "Amazon Leo"
        # is a parent whose husk must NOT fold into its subsidiary. The prefix
        # rule cannot tell these apart — context-aware LLM arbitration can.
        if not options['no_llm']:
            from ingest.tasks.resolve import _llm_pick_candidate
            arbitrated = []
            for fid, fname, cid, cname, cont_n in candidates:
                verdict, _conf = _llm_pick_candidate(
                    fname, Entity.objects.filter(pk=fid).values_list('entity_type', flat=True).first() or 'company',
                    [(cid, cname, 1.0)],
                    subject_context=f'Is the bare name "{fname}" the same organisation as "{cname}"?',
                )
                mark = 'SAME' if verdict else 'DIFFERENT'
                self.stdout.write(f'  [{mark:>9}] "{fname[:38]}" vs "{cname[:38]}"')
                if verdict:
                    arbitrated.append((fid, fname, cid, cname, cont_n))
            candidates = arbitrated
            if not candidates:
                self.stdout.write('LLM arbitration confirmed no true fragments. Nothing to heal.')
                return

        self.stdout.write(f'\n{len(candidates)} empty fragment(s) to heal:\n')
        for fid, fname, cid, cname, cont_n in candidates:
            self.stdout.write(f'  "{fname[:42]:<42}" -> "{cname[:42]:<42}" ({cont_n} assertions)')

        if not options['apply']:
            self.stdout.write(self.style.WARNING('\nDry run — pass --apply to heal.'))
            return

        from ingest.tasks.dedup import execute_merge
        merged = 0
        for fid, fname, cid, cname, cont_n in candidates:
            try:
                execute_merge(
                    cid, fid,
                    rationale={
                        'reason': 'empty_fragment_heal',
                        'fragment_assertions': 0,
                        'container_assertions': cont_n,
                    },
                    performed_by='auto:fragment_heal',
                )
                merged += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'  failed: "{fname}" -> {exc}'))

        self.stdout.write(self.style.SUCCESS(f'\nHealed {merged} fragment(s).'))
