# Synthetic demonstration data

Every seeded retailer and route baseline is invented and explicitly labelled
`data_source="synthetic"`. Names and identifiers include a synthetic marker.
There are no real retailer identities, contact details, brand names, commercial
orders, production data, or copied private schemas in this implementation.

The initial dataset contains ten retailers: two historically active and eight
inactive. The route's monthly revenue/cost baseline is arbitrary demonstration
data. Seeding creates no purchases, commitments, missions, or rewards.

Later purchase events must be explicitly marked `simulated_purchase=true` and
`data_source="simulated"`. Synthetic fixture origin and simulated transaction
status are distinct concepts. Neither observations nor commitments are sales.

Mock AI responses carry `data_source="mock"` and an explicit MOCK uncertainty
statement. They do not represent real Meta inference or verified commercial
facts. Structural/evidence validation does not establish commercial correctness.

**SIMULATED DEMONSTRATION DATA  -  not verified commercial transactions.**
This prototype demonstrates software mechanisms; it does not prove real-world
retailer activation, profitability, or incentive effectiveness.
