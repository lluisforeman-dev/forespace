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
        ],
    },
    'technology': {
        'label': 'Core Technology',
        'nodes': [
            ('chemical_propulsion', 'Chemical Propulsion', 'Rockets using liquid or solid chemical propellants.', ['SpaceX Merlin', 'Aerojet']),
            ('electric_propulsion', 'Electric / Ion Propulsion', 'Ion thrusters, Hall-effect thrusters, gridded ion engines.', ['Safran', 'Starlink V2']),
            ('nuclear_propulsion', 'Nuclear Propulsion', 'Nuclear thermal or electric propulsion — in development.', ['BWX Technologies']),
            ('optical_comms', 'Optical / Laser Communications', 'Free-space laser links between satellites or to ground.', ['Mynaric', 'Tesat']),
            ('sar', 'SAR (Synthetic Aperture Radar)', 'Radar imaging satellites operating in cloudy or night conditions.', ['Capella Space', 'ICEYE']),
            ('hyperspectral', 'Hyperspectral Imaging', 'Satellites capturing many spectral bands beyond RGB for material identification.', ['HySpecIQ']),
            ('reusable_launch', 'Reusable Launch Systems', 'Launch vehicles designed for recovery and re-flight.', ['SpaceX Falcon 9', 'Blue Origin New Shepard']),
            ('rideshare', 'Rideshare / Smallsat Deployment', 'Dedicated rideshare services deploying multiple small satellites per mission.', ['SpaceX Transporter', 'Rocket Lab']),
            ('direct_to_cell', 'Direct-to-Cell Connectivity', 'Satellites communicating directly with standard mobile handsets without special hardware.', ['AST SpaceMobile', 'Starlink']),
            ('gnss_pnt', 'GNSS & PNT Systems', 'Satellite navigation, positioning, timing systems and augmentation services.', ['GMV', 'GSA', 'Septentrio']),
            ('ai_earth_observation', 'AI for Earth Observation', 'Machine learning and computer vision applied to satellite imagery analysis.', ['Satellogic', 'Planet Labs']),
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
    'research_area': {
        'label': 'Research Area',
        'nodes': [
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
        ],
    },
    'funding_type': {
        'label': 'Funding Type',
        'nodes': [
            # ── Equity ────────────────────────────────────────────────────────
            ('equity', 'Equity Investment', 'Any investment in exchange for an ownership stake in the company.', []),
            ('equity.pre_seed', 'Pre-Seed / Friends & Family', 'Very early capital from founders, friends, family, or angels before a formal seed round.', []),
            ('equity.seed', 'Seed Round', 'First institutional equity round, typically from angel groups or early-stage VCs.', []),
            ('equity.series_a', 'Series A', 'First significant venture capital round, typically $2M–$20M.', []),
            ('equity.series_b', 'Series B', 'Scale-up round following product-market fit, typically $15M–$100M.', []),
            ('equity.series_c', 'Series C', 'Growth round for established startups expanding into new markets.', []),
            ('equity.series_d_plus', 'Series D+', 'Late-stage venture rounds (D, E, F…) for companies approaching IPO or at scale.', []),
            ('equity.growth', 'Growth Equity', 'Minority equity investment in mature, profitable or near-profitable companies.', []),
            ('equity.strategic', 'Strategic / Corporate VC', 'Investment from a corporate venture arm or strategic investor (not a traditional VC).', ['Airbus Ventures', 'In-Q-Tel', 'Boeing HorizonX']),
            ('equity.secondary', 'Secondary Sale', 'Purchase of existing shares from early investors or employees, not new capital to the company.', []),
            ('equity.ipo', 'IPO', 'Initial public offering — company lists shares on a stock exchange for the first time.', ['Virgin Galactic', 'Rocket Lab']),
            ('equity.spac', 'SPAC Merger', 'Company goes public via merger with a Special Purpose Acquisition Company.', ['Astra', 'Momentus']),
            ('equity.crowdfunding', 'Equity Crowdfunding', 'Equity investment raised from the public via regulated crowdfunding platforms.', ['Seedrs', 'Republic', 'Crowdcube']),
            # ── Grants ────────────────────────────────────────────────────────
            ('grant', 'Grant / Non-Dilutive Public Funding', 'Public funding awarded without equity dilution.', []),
            ('grant.horizon_europe', 'Horizon Europe', 'EU Framework Programme for Research and Innovation (2021–2027). Calls under Cluster 4 (Space) and others.', ['ESA', 'European Commission']),
            ('grant.esa_programme', 'ESA Programme Funding', 'Funding from ESA programmes: GSTP, ARTES, ScyLight, NAVISP, FAST, BIC, etc.', ['ESA']),
            ('grant.national_agency', 'National Space Agency Grant', 'Grants from national space agencies: UKSA, CNES, DLR, ASI, JAXA, ISRO, KARI, etc.', []),
            ('grant.sbir_sttr', 'SBIR / STTR (US)', 'US Small Business Innovation Research and Technology Transfer programmes from NASA, DoD, NSF.', ['NASA', 'DoD']),
            ('grant.government', 'Government / Ministry Grant', 'Direct grants from ministries, innovation agencies (Innovate UK, Bpifrance, CDTI, etc.).', ['Innovate UK', 'Bpifrance', 'CDTI']),
            ('grant.prize', 'Competition Prize / Award', 'Cash prize won in a technology competition (XPRIZE, ESA BIC prize, etc.).', ['XPRIZE']),
            ('grant.innovation_voucher', 'Innovation Voucher', 'Small non-dilutive voucher for accessing research infrastructure or expertise.', []),
            # ── Non-equity capital ────────────────────────────────────────────
            ('non_equity', 'Non-Equity / Non-Dilutive Capital', 'Capital that is neither a grant nor traditional equity.', []),
            ('non_equity.revenue_based', 'Revenue-Based Financing', 'Capital repaid as a percentage of future revenues, no equity dilution.', []),
            ('non_equity.in_kind', 'In-Kind / Incubation Support', 'Non-cash support: lab access, ESA BIC incubation, co-working, computing credits.', ['ESA BIC']),
            ('non_equity.contract_rd', 'Funded R&D Contract', 'Customer pays company to develop a technology; company retains IP or shares it.', ['ESA', 'DARPA', 'NASA']),
            # ── Debt ─────────────────────────────────────────────────────────
            ('debt', 'Debt Financing', 'Capital borrowed and repaid with interest, no equity dilution.', []),
            ('debt.venture_debt', 'Venture Debt', 'Debt extended to venture-backed startups, often alongside an equity round.', ['Kreos Capital', 'Silicon Valley Bank']),
            ('debt.bank_loan', 'Bank Loan / Credit Facility', 'Traditional commercial bank lending or revolving credit facility.', []),
            ('debt.bond', 'Corporate Bond', 'Debt security issued to investors in public or private markets.', []),
            ('debt.eib_eif', 'EIB / EIF Loan or Guarantee', 'Financing from the European Investment Bank or European Investment Fund.', ['EIB', 'EIF']),
            ('debt.government_loan', 'Government-Backed Loan', 'Loan guaranteed or subsidised by a government or development bank.', []),
            # ── Convertible instruments ───────────────────────────────────────
            ('convertible', 'Convertible Instrument', 'Debt or right that converts into equity at a future round or event.', []),
            ('convertible.safe', 'SAFE Note', 'Simple Agreement for Future Equity — converts at a priced round, no maturity date.', []),
            ('convertible.note', 'Convertible Note', 'Interest-bearing debt that converts to equity at a future round, with maturity date.', []),
            ('convertible.bond', 'Convertible Bond', 'Bond that can be converted to equity at a set price, typically issued by larger companies.', []),
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
