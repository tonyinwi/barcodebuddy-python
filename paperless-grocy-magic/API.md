# Paperless Grocy Magic - API Documentation

## Barcode Scanner Integration API

Diese Endpoints sind für die Integration mit einem Barcode Scanner Add-on gedacht.

### Base URL
- **Direct Access**: `http://<host>:5002`
- **Ingress**: `http://<homeassistant>/hassio/ingress/<addon-slug>`

---

## Endpoints

### 1. Find Alias by Barcode

Prüft, ob ein Barcode bereits einem Alias zugeordnet ist.

**Endpoint:** `GET /api/aliases/barcode/<barcode>`

**Response (Found):**
```json
{
  "success": true,
  "found": true,
  "alias": {
    "receipt_name": "vorderhaxe",
    "grocy_product_id": 123,
    "grocy_product_name": "Schweinshaxe gegart",
    "barcodes": ["4012345678901", "4012345678902"],
    "notes": "Auto-created from REWE receipt"
  }
}
```

**Response (Not Found):**
```json
{
  "success": true,
  "found": false,
  "message": "No alias found for barcode '4012345678901'"
}
```

---

### 2. Add Barcode to Alias

Fügt einen Barcode zu einem existierenden Alias hinzu.

**Endpoint:** `PUT /api/aliases/<receipt_name>/barcode`

**Request Body:**
```json
{
  "barcode": "4012345678901"
}
```

**Response (Success):**
```json
{
  "success": true,
  "message": "Barcode '4012345678901' added to alias 'vorderhaxe'",
  "alias": {
    "receipt_name": "vorderhaxe",
    "grocy_product_id": 123,
    "grocy_product_name": "Schweinshaxe gegart",
    "barcodes": ["4012345678901"],
    "notes": "Auto-created from REWE receipt"
  }
}
```

**Response (Alias Not Found - 404):**
```json
{
  "success": false,
  "error": "Alias 'vorderhaxe' not found"
}
```

**Response (Barcode Already Used - 409):**
```json
{
  "success": false,
  "error": "Barcode '4012345678901' is already used by alias 'other_product' (Grocy product: Other Product Name)"
}
```

**Response (Already Exists):**
```json
{
  "success": true,
  "message": "Barcode '4012345678901' already exists for alias 'vorderhaxe'",
  "alias": { ... }
}
```

---

### 3. Remove Barcode from Alias

Entfernt einen Barcode von einem Alias.

**Endpoint:** `DELETE /api/aliases/<receipt_name>/barcode/<barcode>`

**Response (Success):**
```json
{
  "success": true,
  "message": "Barcode '4012345678901' removed from alias 'vorderhaxe'",
  "alias": {
    "receipt_name": "vorderhaxe",
    "grocy_product_id": 123,
    "grocy_product_name": "Schweinshaxe gegart",
    "barcodes": [],
    "notes": "Auto-created from REWE receipt"
  }
}
```

**Response (Alias Not Found - 404):**
```json
{
  "success": false,
  "error": "Alias 'vorderhaxe' not found"
}
```

**Response (Barcode Not Found - 404):**
```json
{
  "success": false,
  "error": "Barcode '4012345678901' not found in alias 'vorderhaxe'"
}
```

---

### 4. Get All Aliases

Liste alle Aliase auf (nützlich für Debugging).

**Endpoint:** `GET /api/aliases`

**Response:**
```json
{
  "success": true,
  "count": 2,
  "aliases": [
    {
      "receipt_name": "vorderhaxe",
      "grocy_product_id": 123,
      "grocy_product_name": "Schweinshaxe gegart",
      "barcodes": ["4012345678901"],
      "notes": "Auto-created from REWE receipt"
    },
    {
      "receipt_name": "bierschinken",
      "grocy_product_id": 124,
      "grocy_product_name": "Bierschinken",
      "barcodes": [],
      "notes": "Auto-created from REWE receipt"
    }
  ]
}
```

---

### 5. Create New Alias

Erstellt einen neuen Alias (für Produkte, die noch nicht in Grocy existieren).

**Endpoint:** `POST /api/aliases`

**Request Body:**
```json
{
  "receipt_name": "vorderhaxe",
  "grocy_product_id": 123,
  "grocy_product_name": "Schweinshaxe gegart",
  "barcodes": ["4012345678901", "4012345678902"],
  "notes": "Added via Barcode Scanner"
}
```

**Response (Success):**
```json
{
  "success": true,
  "message": "Alias added for 'vorderhaxe'"
}
```

---

## Workflow für Barcode Scanner Add-on

### Szenario 1: Barcode scannen für existierendes Produkt

```
1. User scannt Barcode: 4012345678901

2. Scanner Add-on: GET /api/aliases/barcode/4012345678901
   → Found: receipt_name="vorderhaxe", grocy_product_id=123

3. Scanner Add-on kann direkt zu Grocy Stock hinzufügen:
   POST /grocy/api/stock/products/123/add
```

### Szenario 2: Barcode scannen für Produkt ohne Barcode

```
1. User scannt Barcode: 4012345678901

2. Scanner Add-on: GET /api/aliases/barcode/4012345678901
   → Not Found

3. Scanner Add-on zeigt Liste aller Aliase:
   GET /api/aliases
   → Zeigt: vorderhaxe, bierschinken, etc.

4. User wählt: "vorderhaxe" (ID 123)

5. Scanner Add-on fügt Barcode hinzu:
   PUT /api/aliases/vorderhaxe/barcode
   Body: {"barcode": "4012345678901"}

6. Nächstes Mal: Barcode wird automatisch erkannt!
```

### Szenario 3: Neues Produkt über Barcode erstellen

```
1. User scannt Barcode: 4012345678901

2. Scanner Add-on: GET /api/aliases/barcode/4012345678901
   → Not Found

3. User wählt: "Neues Produkt erstellen"
   Input: Name="Schweinshaxe gegart"

4. Scanner Add-on erstellt Produkt in Grocy:
   POST /grocy/api/objects/products
   → Erhält: product_id=123

5. Scanner Add-on erstellt Alias mit Barcode:
   POST /api/aliases
   Body: {
     "receipt_name": "schweinshaxe gegart",
     "grocy_product_id": 123,
     "grocy_product_name": "Schweinshaxe gegart",
     "barcodes": ["4012345678901"],
     "notes": "Created via Barcode Scanner"
   }
```

---

## Shared Storage

Wenn beide Add-ons `/share` Storage nutzen:
- **Paperless Grocy Magic** erstellt Auto-Aliase beim Receipt-Processing
- **Barcode Scanner** kann diese Aliase lesen und Barcodes hinzufügen
- Beide Add-ons teilen sich dieselbe Alias-Datenbank!

**Config in beiden Add-ons:**
```yaml
alias_storage_location: "shared"
```

**Pfad:** `/share/paperless-grocy-magic/product_aliases.json`

---

## Error Codes

- **200** - Success
- **400** - Bad Request (missing fields, empty barcode)
- **404** - Not Found (alias or barcode not found)
- **409** - Conflict (barcode already used by another alias)
- **500** - Internal Server Error

---

## Beispiel: Python Client

```python
import requests

BASE_URL = "http://homeassistant:5002/api"

def find_product_by_barcode(barcode):
    """Sucht Produkt via Barcode."""
    response = requests.get(f"{BASE_URL}/aliases/barcode/{barcode}")
    data = response.json()

    if data['found']:
        alias = data['alias']
        print(f"Found: {alias['grocy_product_name']} (ID {alias['grocy_product_id']})")
        return alias['grocy_product_id']
    else:
        print("Not found")
        return None

def add_barcode_to_product(receipt_name, barcode):
    """Fügt Barcode zu Alias hinzu."""
    response = requests.put(
        f"{BASE_URL}/aliases/{receipt_name}/barcode",
        json={"barcode": barcode}
    )

    if response.status_code == 200:
        print("Barcode added successfully!")
    elif response.status_code == 409:
        print("Barcode already used by another product!")
    else:
        print(f"Error: {response.json()['error']}")

# Usage
product_id = find_product_by_barcode("4012345678901")
if not product_id:
    add_barcode_to_product("vorderhaxe", "4012345678901")
```

---

## Curl Beispiele

```bash
# Find by barcode
curl http://localhost:5002/api/aliases/barcode/4012345678901

# Add barcode to alias
curl -X PUT http://localhost:5002/api/aliases/vorderhaxe/barcode \
  -H "Content-Type: application/json" \
  -d '{"barcode": "4012345678901"}'

# Remove barcode from alias
curl -X DELETE http://localhost:5002/api/aliases/vorderhaxe/barcode/4012345678901

# Get all aliases
curl http://localhost:5002/api/aliases
```
