"""Seed research_area taxonomy facet and research-related attributes."""
from django.db import migrations

RESEARCH_NODES = [
    ('propulsion', 'Propulsion Research', 'Academic and applied research into spacecraft and launch vehicle propulsion systems.', []),
    ('propulsion.chemical', 'Chemical Propulsion Research', 'Research into liquid, solid, and hybrid chemical rocket propulsion.', ['NASA Marshall', 'DLR']),
    ('propulsion.electric_ion', 'Electric & Ion Propulsion Research', 'Research into ion thrusters, Hall-effect thrusters, and other electric propulsion.', ['JPL', 'University of Michigan']),
    ('propulsion.nuclear', 'Nuclear Propulsion Research', 'Research into nuclear thermal, nuclear electric, and pulsed nuclear propulsion.', ['NASA GRC', 'Los Alamos', 'USNC-Tech']),
    ('propulsion.solar_sail', 'Solar Sail & Laser Propulsion', 'Photon-pressure and directed-energy propulsion concepts.', ['The Planetary Society', 'Breakthrough Starshot']),
    ('structures_materials', 'Structures & Materials', 'Research into spacecraft structures, thermal protection, and advanced materials.', []),
    ('structures_materials.thermal_protection', 'Thermal Protection Systems', 'Heat shields, ablatives, and reusable thermal protection for re-entry vehicles.', ['NASA Ames', 'ESA']),
    ('structures_materials.composites', 'Composite Structures', 'Carbon-fibre, metal matrix, and other composite materials for space applications.', []),
    ('structures_materials.additive_manufacturing', 'Additive Manufacturing', 'Metal and polymer 3D printing for rocket and spacecraft components.', ['NASA', 'ESA']),
    ('autonomous_systems', 'Autonomous Systems', 'Research into spacecraft autonomy, guidance, and artificial intelligence.', []),
    ('autonomous_systems.gnc', 'Guidance, Navigation & Control', 'Attitude determination, orbit determination, and autonomous manoeuvring.', ['MIT', 'Stanford']),
    ('autonomous_systems.ai_ml', 'AI & Machine Learning for Space', 'Machine learning applied to mission planning, fault detection, and data analysis.', ['JPL', 'ESA']),
    ('autonomous_systems.robotics', 'Space Robotics & Manipulation', 'Robotic arms, rovers, and autonomous assembly in space or on planetary surfaces.', ['NASA JPL', 'DLR']),
    ('earth_observation', 'Earth Observation Science', 'Research into remote sensing methods and applications.', []),
    ('earth_observation.sar_radar', 'SAR & Radar Remote Sensing', 'Synthetic aperture radar, microwave radiometry, and altimetry.', ['ESA', 'DLR']),
    ('earth_observation.optical', 'Optical & Multispectral Imaging', 'Visible, near-IR, and multispectral satellite imaging science.', ['CNES', 'NASA GSFC']),
    ('earth_observation.hyperspectral', 'Hyperspectral Sensing', 'Narrowband spectral imaging for mineral, vegetation, and atmospheric analysis.', []),
    ('earth_observation.gnss_r', 'GNSS Reflectometry', 'Using reflected GNSS signals for ocean, land, and ice surface sensing.', ['ESA', 'NOAA']),
    ('communications', 'Space Communications Research', 'Research into space communication systems, protocols, and architectures.', []),
    ('communications.laser_optical', 'Free-Space Optical Communications', 'Laser-based space-to-ground and inter-satellite links.', ['NASA LLCD', 'ESA LCOT']),
    ('communications.quantum', 'Quantum Communications', 'Quantum key distribution and entanglement-based communication from orbit.', ['Chinese Academy of Sciences', 'ESA']),
    ('communications.delay_tolerant', 'Delay-Tolerant Networking', 'DTN protocols for deep-space and disruption-prone space communications.', ['NASA', 'ESA']),
    ('space_environment', 'Space Environment & Sustainability', 'Research into the space environment and its impact on missions and the ecosystem.', []),
    ('space_environment.debris', 'Space Debris & Mitigation', 'Debris tracking, collision avoidance, active removal, and end-of-life disposal.', ['ESA', 'NASA', 'Astroscale']),
    ('space_environment.space_weather', 'Space Weather', 'Solar wind, geomagnetic storms, radiation belts, and their effects on spacecraft.', ['NOAA', 'ESA']),
    ('space_environment.radiation', 'Radiation Effects on Electronics', 'Single-event effects, total ionising dose, and radiation-hardened design.', ['ESA ESTEC', 'NASA GSFC']),
    ('life_sciences', 'Space Life Sciences', 'Research into biology, medicine, and human factors for long-duration spaceflight.', []),
    ('life_sciences.human_factors', 'Human Factors & Habitability', 'Ergonomics, psychology, and crew performance in space environments.', ['NASA JSC', 'ESA EAC']),
    ('life_sciences.astrobiology', 'Astrobiology & Biosignatures', 'Search for life, extremophiles, and planetary protection.', ['NASA Astrobiology Institute']),
    ('life_sciences.biomedical', 'Space Medicine & Countermeasures', 'Bone loss, muscle atrophy, cardiovascular changes, and countermeasures for microgravity.', ['ESA', 'Roscosmos']),
    ('planetary_science', 'Planetary Science', 'Research into the Moon, Mars, and other solar system bodies.', []),
    ('planetary_science.lunar', 'Lunar Science & ISRU', 'Lunar geology, resource extraction, in-situ resource utilisation.', ['NASA', 'ESA', 'JAXA']),
    ('planetary_science.mars', 'Mars Exploration', 'Mars atmosphere, geology, and human mission enabling research.', ['NASA JPL', 'ESA']),
    ('planetary_science.small_bodies', 'Asteroids & Comets', 'Characterisation and potential resource extraction from small solar system bodies.', ['NASA OSIRIS-REx', 'ESA Hera']),
    ('policy_economics', 'Space Policy & Economics', 'Research into space governance, law, market dynamics, and sustainability frameworks.', []),
    ('policy_economics.law', 'Space Law & Governance', 'International space law, licensing, liability, and orbital slot regulation.', []),
    ('policy_economics.markets', 'Space Market Economics', 'Commercial space market analysis, investment trends, and economic modelling.', []),
    ('policy_economics.sustainability', 'Long-Term Space Sustainability', 'Space traffic management, frequency coordination, and sustainability guidelines.', ['UN COPUOS', 'ITU']),
]

NEW_ATTRIBUTES = [
    {
        'key': 'publication_count',
        'entity_type': 'company',
        'label': 'Space-Related Publications',
        'datatype': 'int',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 180,
        'description': (
            'Total number of peer-reviewed papers, conference proceedings (IAC, AIAA, ESA), '
            'or preprints published by the entity on space-related topics. '
            'Extract for companies, universities, research institutes, and individual researchers.'
        ),
    },
    {
        'key': 'research_focus',
        'entity_type': 'company',
        'label': 'Research Focus',
        'datatype': 'text',
        'cardinality': 'single',
        'is_projected': True,
        'is_promoted': False,
        'volatility_days': 365,
        'description': (
            'Primary academic or R&D research area of the entity, in plain English. '
            'Examples: "nuclear propulsion", "space debris removal", "SAR remote sensing". '
            'Extract for universities, research institutes, and R&D-heavy companies.'
        ),
    },
]


def seed_research(apps, schema_editor):
    Taxonomy = apps.get_model('core', 'Taxonomy')
    TaxonomyNode = apps.get_model('core', 'TaxonomyNode')
    AttributeDef = apps.get_model('core', 'AttributeDef')

    taxonomy, _ = Taxonomy.objects.get_or_create(
        key='research_area',
        defaults={'version': 1, 'status': 'active'},
    )
    for path, label, definition, examples in RESEARCH_NODES:
        TaxonomyNode.objects.get_or_create(
            taxonomy=taxonomy,
            path=path,
            defaults={'label': label, 'definition': definition, 'examples': examples, 'status': 'active'},
        )

    for spec in NEW_ATTRIBUTES:
        AttributeDef.objects.get_or_create(
            key=spec['key'],
            defaults={k: v for k, v in spec.items() if k != 'key'},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_merge_technology_into_value_chain'),
    ]

    operations = [
        migrations.RunPython(seed_research, migrations.RunPython.noop),
    ]
