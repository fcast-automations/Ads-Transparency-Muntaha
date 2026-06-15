# config.py

# --- Google Sheets ---
SPREADSHEETS = {
    'primary': '1NDp5gwAYsdj-tC4mNqSDECJ9eXwD6fG4zFK6vFcy17E',
    'secondary': '11wWrqF2nKbO415FWELvVwEHO3zDtZh2vPme3j9GeM34',
}
SPREADSHEET_ID = SPREADSHEETS['secondary']
WORKSHEET_NAME = 'Ad Scraper'                   
CREDENTIALS_FILE = 'creds.json'             

# --- Scraper settings ---
HEADLESS = False      

WAIT_TIMEOUT = 20     # Seconds to wait for the video to load after clicking play
