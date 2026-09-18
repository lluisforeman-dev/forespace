"""Merge 'technology' taxonomy facet into 'value_chain' as technology.* sub-nodes."""
from django.db import migrations

# Old path (in technology taxonomy) → new path (in value_chain taxonomy)
PATH_MAP = {
    'chemical_propulsion': 'technology.chemical_propulsion',
    'electric_propulsion':  'technology.electric_propulsion',
    'nuclear_propulsion':   'technology.nuclear_propulsion',
    'optical_comms':        'technology.optical_comms',
    'sar':                  'technology.sar',
    'hyperspectral':        'technology.hyperspectral',
    'reusable_launch':      'technology.reusable_launch',
    'rideshare':            'technology.rideshare',
    'direct_to_cell':       'technology.direct_to_cell',
}

NEW_NODES = [
    ('technology', 'Core Technology', 'Classification by the primary enabling technology the company develops or deploys.', []),
    ('technology.chemical_propulsion', 'Chemical Propulsion', 'Rockets using liquid or solid chemical propellants.', ['SpaceX Merlin', 'Aerojet']),
    ('technology.electric_propulsion', 'Electric / Ion Propulsion', 'Ion thrusters, Hall-effect thrusters, gridded ion engines.', ['Safran', 'Starlink V2']),
    ('technology.nuclear_propulsion', 'Nuclear Propulsion', 'Nuclear thermal or electric propulsion — in development.', ['BWX Technologies']),
    ('technology.optical_comms', 'Optical / Laser Communications', 'Free-space laser links between satellites or to ground.', ['Mynaric', 'Tesat']),
    ('technology.sar', 'SAR (Synthetic Aperture Radar)', 'Radar imaging satellites operating in cloudy or night conditions.', ['Capella Space', 'ICEYE']),
    ('technology.hyperspectral', 'Hyperspectral Imaging', 'Satellites capturing many spectral bands beyond RGB for material identification.', ['HySpecIQ']),
    ('technology.reusable_launch', 'Reusable Launch Systems', 'Launch vehicles designed for recovery and re-flight.', ['SpaceX Falcon 9', 'Blue Origin New Shepard']),
    ('technology.rideshare', 'Rideshare / Smallsat Deployment', 'Dedicated rideshare services deploying multiple small satellites per mission.', ['SpaceX Transporter', 'Rocket Lab']),
    ('technology.direct_to_cell', 'Direct-to-Cell Connectivity', 'Satellites communicating directly with standard mobile handsets without special hardware.', ['AST SpaceMobile', 'Starlink']),
]


def merge_technology(apps, schema_editor):
    Taxonomy = apps.get_model('core', 'Taxonomy')
    TaxonomyNode = apps.get_model('core', 'TaxonomyNode')
    Classification = apps.get_model('core', 'Classification')

    # Get (or skip if not seeded) the value_chain taxonomy
    try:
        value_chain = Taxonomy.objects.get(key='value_chain')
    except Taxonomy.DoesNotExist:
        return  # taxonomy not seeded yet — seed_taxonomy will handle it

    # Create new technology.* nodes inside value_chain
    new_node_map = {}  # old_path → new TaxonomyNode
    for path, label, definition, examples in NEW_NODES:
        node, _ = TaxonomyNode.objects.get_or_create(
            taxonomy=value_chain,
            path=path,
            defaults={'label': label, 'definition': definition, 'examples': examples, 'status': 'active'},
        )
        new_node_map[path] = node

    # Remap existing Classifications from old technology nodes → new value_chain nodes
    try:
        tech_taxonomy = Taxonomy.objects.get(key='technology')
    except Taxonomy.DoesNotExist:
        return  # nothing to migrate

    for old_path, new_path in PATH_MAP.items():
        try:
            old_node = TaxonomyNode.objects.get(taxonomy=tech_taxonomy, path=old_path)
        except TaxonomyNode.DoesNotExist:
            continue
        new_node = new_node_map.get(new_path)
        if new_node:
            Classification.objects.filter(node=old_node).update(node=new_node)
        old_node.status = 'deprecated'
        old_node.save()

    # Deprecate the technology taxonomy itself
    tech_taxonomy.status = 'deprecated'
    tech_taxonomy.save()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_add_university_entity_type'),
    ]

    operations = [
        migrations.RunPython(merge_technology, migrations.RunPython.noop),
    ]
