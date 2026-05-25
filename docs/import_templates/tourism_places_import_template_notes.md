# Tourism Places Import Template

Use `tourism_places_import_template.csv` for Tourism Office-verified public tourism content.

Accepted `category` values:

- `dining`
- `tourist_spot`
- `landmark`
- `public_facility`
- `shopping_local_products`
- `approved_stay`
- `other_tourism_place`

Importer behavior:

- Rows are imported only when `is_published` is truthy, such as `true`, `yes`, `1`, `published`, or `active`.
- Public map markers are created only when verified `latitude` and `longitude` are provided.
- Tourist spot, landmark, attraction, and nature rows also create/update published `TourismInformation` records.
- Dining, public facility, shopping/local products, approved stay, and other place rows create/update public `MapBookmark` records when coordinates are available.
- The importer does not create users, owners, bookings, payment links, migrations, or database schema changes.
- Third-party accommodation booking links are not part of this template.

Recommended dry-run command:

```powershell
python manage.py import_tourism_places --csv docs/import_templates/tourism_places_import_template.csv --dry-run
```
