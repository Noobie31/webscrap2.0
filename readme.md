# MyAgedCare Scraper

A Python scraper for MyAgedCare provider data with multi-level iteration through locations and search types.

## 🚀 Setup

### 1. Clone the Repository
```bash
git clone https://github.com/Noobie31/webscrap2.0
cd webscrap2.0
```

### 2. Create and Activate Virtual Environment
```bash
python -m venv venv
```

**Windows**
```bash
venv\Scripts\activate
```

**macOS / Linux**
```bash
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r req.txt
playwright install
```

### 4. Add Postcodes Data
Place your `postcodes.json` file in:

```
/input/postcodes.json
```

---

## ⚙ Configuration

Edit these variables in **test.py**:

```python
DISTANCE = "250"       # Distance filter: 5, 10, 20, 50, 250 — or "" for no filter
LINK_PER_SEARCH = 2    # Set to None or 0 for all links, or specify a limit
```

---

## ▶️ Run the Scraper
```bash
python test.py
```

---

## ⭐ Features

- **3-level iteration:** Locations → Search Types → Provider Pages  
- **Duplicate prevention:** Skips providers with previously stored telephone numbers  
- **Progress saving:** Automatically continues from the last processed location if interrupted  
- **CSV append mode:** Existing data is preserved across runs

