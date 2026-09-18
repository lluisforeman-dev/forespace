"""Seed the space-industry taxonomy — 6 facets, ~40 nodes (§6).

Each facet is a separate Taxonomy row. Nodes use ltree dotted paths.
The LLM classification prompt reads node `definition` directly — write carefully.
"""
from django.core.management.base import BaseCommand
from core.models import Taxonomy, TaxonomyNode

# Format: (path, label, definition, examples)
# Paths are relative to the facet root (no facet prefix).
FACETS = {
    'value_chain': {
        'label': 'Value Chain Position',
        'nodes': [
            ('upstream', 'Upstream', 'Activities closest to hardware production: manufacturing rockets, satellites, propulsion systems, and ground segment equipment.', ['SpaceX', 'Airbus Defence and Space', 'Rocket Lab']),
            ('upstream.launch', 'Launch Services', 'Companies that provide launch vehicle services to place payloads into orbit.', ['SpaceX', 'Rocket Lab', 'Arianespace']),
            ('upstream.launch.small_lift', 'Small Lift Launch (<1,000 kg LEO)', 'Launch vehicles with payload capacity under 1,000 kg to LEO.', ['Rocket Lab Electron', 'Virgin Orbit']),
            ('upstream.launch.medium_lift', 'Medium Lift Launch (1,000–10,000 kg LEO)', 'Launch vehicles delivering 1,000–10,000 kg to LEO.', ['SpaceX Falcon 9', 'ULA Vulcan']),
            ('upstream.launch.heavy_lift', 'Heavy Lift Launch (>10,000 kg LEO)', 'Launch vehicles delivering more than 10,000 kg to LEO.', ['SpaceX Falcon Heavy', 'Ariane 5']),
            ('upstream.satellite_manufacturing', 'Satellite Manufacturing', 'Design and manufacture of satellite buses, payloads, and subsystems.', ['Airbus', 'Thales Alenia', 'GeoOptics']),
            ('upstream.propulsion', 'Propulsion Systems', 'Manufacturers of rocket engines and spacecraft propulsion units.', ['Aerojet Rocketdyne', 'Bradford ECAPS']),
            ('upstream.ground_segment', 'Ground Segment Hardware', 'Antennas, ground stations, tracking and control equipment.', ['Kongsberg', 'Viasat']),
            ('midstream', 'Midstream', 'Processing, aggregating, and distributing space-derived data and connectivity.', []),
            ('midstream.data_processing', 'Data Processing', 'Companies that process raw satellite data (imagery, signals) into usable products.', ['Planet Labs', 'Satellogic']),
            ('midstream.connectivity', 'Connectivity Services', 'Satellite internet and communications service providers (broadband, IoT, M2M).', ['Starlink', 'OneWeb', 'Iridium']),
            ('midstream.ground_ops', 'Ground Operations & Network', 'Satellite network management, TT&C, spectrum coordination.', ['KSAT', 'AWS Ground Station']),
            ('downstream', 'Downstream', 'Customers and value-added resellers consuming space services and data.', []),
            ('downstream.earth_observation', 'Earth Observation Analytics', 'Applications built on satellite imagery: agriculture, forestry, urban analytics.', ['Descartes Labs', 'Orbital Insight']),
            ('downstream.navigation', 'Navigation & Positioning Services', 'GNSS-dependent services, precision agriculture, autonomous vehicle positioning.', ['HERE Technologies']),
            ('downstream.satcom', 'Satellite Communications Applications', 'End-user applications for satellite voice, broadband, and video services.', ['ViaSat', 'Inmarsat']),
            ('downstream.space_tourism', 'Space Tourism & Human Spaceflight', 'Companies offering crewed suborbital or orbital experiences.', ['Blue Origin', 'Virgin Galactic', 'Axiom Space']),
            ('adjacent', 'Adjacent & Dual-Use', 'Companies spanning space and other industries, or providing enabling technologies.', []),
            ('adjacent.in_space_services', 'In-Space Services', 'On-orbit refuelling, servicing, debris removal, and space logistics.', ['Astroscale', 'Northrop Grumman MEV']),
            ('adjacent.space_defense', 'Defence Space', 'Military and intelligence satellite programmes, space domain awareness.', ['Northrop Grumman', 'L3Harris']),
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
        ],
    },
    'orbit_regime': {
        'label': 'Orbit Regime',
        'nodes': [
            ('vleo', 'VLEO (<400 km)', 'Very Low Earth Orbit — below 400 km altitude. Requires frequent orbital maintenance.', []),
            ('leo', 'LEO (400–2,000 km)', 'Low Earth Orbit — the most common regime for imaging, broadband constellations, ISS.', ['Starlink', 'Planet Labs']),
            ('meo', 'MEO (2,000–35,786 km)', 'Medium Earth Orbit — GNSS constellations (GPS, Galileo, Beidou) and some broadband.', ['O3b mPOWER']),
            ('geo', 'GEO (35,786 km)', 'Geostationary orbit — appears fixed above one point. Traditional broadcast and broadband.', ['Intelsat', 'SES']),
            ('heo', 'HEO (highly elliptical)', 'Highly Elliptical Orbit — used for Arctic coverage and some observation missions.', ['Molniya']),
            ('cislunar', 'Cislunar', 'The region between Earth and the Moon including Lunar orbit. Artemis-era programmes.', ['NASA Artemis', 'Astrobotic']),
            ('interplanetary', 'Interplanetary', 'Missions beyond cislunar space: Mars, asteroids, outer planets.', ['SpaceX Starship', 'Rocket Lab CAPSTONE']),
        ],
    },
    'customer_type': {
        'label': 'Primary Customer Type',
        'nodes': [
            ('government', 'Government / Military', 'Primary customers are national governments, space agencies, or defence departments.', ['Northrop Grumman', 'Sierra Space']),
            ('commercial', 'Commercial', 'Sells primarily to private companies and enterprises.', ['SpaceX Commercial', 'Planet Labs']),
            ('institutional', 'Institutional', 'Serves research institutions, universities, and international bodies.', ['ESA ESRIN suppliers']),
            ('consumer', 'Consumer', 'End products or services sold directly to individual consumers.', ['Virgin Galactic', 'Dish Network']),
            ('mixed', 'Mixed / Dual-Use', 'Significant revenue from both government and commercial customers.', ['Maxar', 'Airbus DS']),
        ],
    },
    'maturity': {
        'label': 'Company Maturity Stage',
        'nodes': [
            ('concept', 'Concept / Pre-seed', 'Company at idea or pre-seed stage — no operational product or revenue.', []),
            ('development', 'Development / Early Stage', 'Active R&D, prototype, or pre-commercial stage. Typically Series A or earlier.', []),
            ('operational', 'Operational / Growth', 'Has a deployed product or service generating revenue. Series B+.', ['Rocket Lab', 'Planet Labs']),
            ('heritage', 'Heritage / Established', 'Established company with decades of operational history.', ['Boeing', 'Lockheed Martin', 'Arianespace']),
        ],
    },
    'adjacent_sector': {
        'label': 'Adjacent Industry Sector',
        'nodes': [
            ('defense', 'Defence & Security', 'Military, intelligence, and national security applications.', []),
            ('telecom', 'Telecommunications', 'Ground-based telecoms operators integrating satellite capacity.', []),
            ('agriculture', 'Precision Agriculture', 'Crop monitoring, yield prediction, soil analysis from orbit.', []),
            ('maritime', 'Maritime & Shipping', 'AIS tracking, weather routing, vessel monitoring.', []),
            ('finance', 'Finance & Insurance', 'Satellite data for supply-chain finance, commodity pricing, risk modelling.', []),
            ('energy', 'Energy & Utilities', 'Pipeline monitoring, solar farm assessment, oil & gas from space.', []),
            ('weather', 'Weather & Climate', 'Commercial weather data, atmospheric monitoring, climate services.', []),
        ],
    },
}


class Command(BaseCommand):
    help = 'Seed space-industry taxonomy — 6 facets, ~40 nodes.'

    def add_arguments(self, parser):
        parser.add_argument('--overwrite', action='store_true')

    def handle(self, *args, **options):
        total_created = total_updated = total_skipped = 0

        for facet_key, facet_data in FACETS.items():
            taxonomy, t_new = Taxonomy.objects.get_or_create(
                key=facet_key,
                defaults={'version': 1, 'status': 'active'},
            )
            if not t_new and options['overwrite']:
                taxonomy.status = 'active'
                taxonomy.save()

            for path, label, definition, examples in facet_data['nodes']:
                node, n_new = TaxonomyNode.objects.get_or_create(
                    taxonomy=taxonomy,
                    path=path,
                    defaults={'label': label, 'definition': definition, 'examples': examples},
                )
                if n_new:
                    total_created += 1
                elif options['overwrite']:
                    node.label = label
                    node.definition = definition
                    node.examples = examples
                    node.save()
                    total_updated += 1
                else:
                    total_skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'seed_taxonomy: {total_created} created, {total_updated} updated, {total_skipped} skipped.'
        ))
