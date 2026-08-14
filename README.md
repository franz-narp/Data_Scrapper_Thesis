# Davao City Flood Report Scraper

A Python script that scrapes public Facebook posts related to flooding in Davao City, Philippines. It uses undetected-chromedriver to search Facebook for flood-related keywords in English, Filipino, and Bisaya, then extracts, filters, and classifies the results.

## Requirements

- Python 3.10 or higher
- Google Chrome browser installed
- A Facebook account

## Installation

1. Clone or download this project.

2. Install the Python dependencies:

```
pip install -r requirements.txt
```

## How to Use

### Step 1: Log in to Facebook (one time only)

Run the script with the --login flag:

```
python davao_flood_scraper.py --login
```

A Chrome window will open. Log in to your Facebook account normally. Once you see your newsfeed, go back to the terminal and press Ctrl+C. Your session is saved in a local folder called fb_chrome_profile and will be reused on future runs.

### Step 2: Run the scraper

```
python davao_flood_scraper.py
```

The script will:

1. Open Chrome using your saved session.
2. Search Facebook for flood-related keywords one by one.
3. Scroll through results and expand truncated posts.
4. Filter posts for flood relevance.
5. Classify each post by urgency level (NORMAL, HIGH, or SITREP).
6. Detect mentioned Davao barangays and areas.
7. Save everything to JSON and CSV files.

You can stop the script at any time with Ctrl+C. It will save whatever it has collected so far.

## Command Line Options

```
python davao_flood_scraper.py --login          Open browser for manual Facebook login
python davao_flood_scraper.py --headless       Run without a visible browser window
python davao_flood_scraper.py --scrolls 80     Scroll 80 times per query (default is 50)
python davao_flood_scraper.py --output-dir ./results   Save output to a specific folder
python davao_flood_scraper.py --profile-dir ./my_profile   Use a custom Chrome profile path
```

## Output

The script produces two files:

- davao_fb_flood_reports.json
- davao_fb_flood_reports.csv

Each record contains the following fields:

| Field              | Description                                      |
|--------------------|--------------------------------------------------|
| post_id            | A short hash used to identify the post           |
| timestamp          | The timestamp shown on the Facebook post         |
| author             | Name of the person or page that posted           |
| full_text          | The complete text content of the post            |
| media_urls         | URLs of images attached to the post              |
| detected_locations | Davao areas mentioned in the post                |
| urgency_level      | NORMAL, HIGH, or SITREP                          |
| post_url           | Direct link to the post on Facebook              |
| scraped_at         | When the script collected this post (UTC)        |

## Search Keywords

The script searches for posts using keywords in three languages:

- Bisaya: baha, nagbaha, lunop, taas ang tubig, lapok, tabang
- Filipino: tulong, evacuation, saklolo
- English: flood, flooding, rescue, stranded, emergency

It also runs area-specific searches for locations like Matina Pangi, Buhangin, Bangkal, Bucana, Tigatto, Bunawan, Talomo, Ma-a, and Jade Valley.

## Monitored Areas

The script looks for mentions of these Davao locations in post text:

Jade Valley, Matina Pangi, Matina Crossing, Maa, Ma-a, Buhangin, Bangkal, Tigatto, Bucana, Talomo, Bunawan, Waan, Juliville, San Rafael, El Rio, Toril, Calinan, Panacan, Sasa, Agdao, Poblacion, Catalunan, Mintal, Tugbok, Bago Aplaya, Lanang, Bajada

## Urgency Classification

- SITREP: Post contains two or more urgency keywords (rescue, stranded, emergency, etc.)
- HIGH: Post contains at least one urgency keyword or two or more flood keywords
- NORMAL: Post mentions flooding but without urgent language

## Notes

- The script adds random pauses between actions to avoid triggering Facebook rate limits.
- Posts are deduplicated by content hash and URL so the same post will not appear twice.
- The Chrome profile folder (fb_chrome_profile) stores your login session. Do not share it.
- This tool is intended for disaster monitoring and research purposes.
