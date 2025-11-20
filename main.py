import asyncio
import json
import csv
import re
import os
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Set
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Page, BrowserContext

# ================= CONFIG =================
BASE_URL = "https://www.myagedcare.gov.au/find-a-provider/search/results"
POSTCODES_FILE = "input/postcodes.json"
SEARCH_TYPES = ["aged-care-homes", "help-at-home"]
HELP_AT_HOME_SERVICES = [
    "Assistive technology prescription and clinical support",
    "Client advisory services", 
    "Community and centre-based respite",
    "Mobility products",
    "Self-care products"
]
DISTANCE = "250"  # Distance in km 5, 10, 20, 50, 250 - set to empty string "" for no distance filter
LINK_PER_SEARCH = 2  # Set to None or 0 to scrape all links, or set a specific number
HEADLESS = False
SLOW_MO_MS = 300
NAV_RETRIES = 3
# ==========================================

@dataclass
class ProviderData:
    company_name: str
    address: str
    suburb: str
    state: str
    postcode: str
    telephone: str
    email: str
    website: str
    search_type: str
    search_location: str
    result_url: str

class MyAgedCareScraper:
    def __init__(self):
        self.results = []
        self.locations = self.load_locations()
        self.existing_telephones = self.load_existing_telephones()
    
    def load_locations(self) -> List[Dict[str, Any]]:
        """Load locations from postcodes.json file"""
        try:
            with open(POSTCODES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"✅ Loaded {len(data)} locations from {POSTCODES_FILE}")
                return data
        except FileNotFoundError:
            print(f"❌ Error: {POSTCODES_FILE} not found in root directory")
            return []
        except json.JSONDecodeError:
            print(f"❌ Error: Invalid JSON in {POSTCODES_FILE}")
            return []
    
    def load_existing_telephones(self) -> Set[str]:
        """Load existing telephone numbers from output.csv to avoid duplicates"""
        telephones = set()
        if os.path.exists('output/output.csv'):
            try:
                with open('output/output.csv', 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('telephone') and row['telephone'].strip():
                            # Clean and normalize telephone number
                            clean_tel = self.clean_telephone(row['telephone'])
                            if clean_tel:
                                telephones.add(clean_tel)
                print(f"✅ Loaded {len(telephones)} existing telephone numbers from output.csv")
            except Exception as e:
                print(f"⚠️ Could not read existing output.csv: {e}")
        else:
            print("ℹ️ output.csv does not exist yet - starting fresh")
        return telephones
    
    def clean_telephone(self, telephone: str) -> str:
        """Clean and normalize telephone number for comparison"""
        if not telephone:
            return ""
        # Remove spaces, dashes, and parentheses
        cleaned = re.sub(r'[\s\-\(\)]', '', telephone.strip())
        return cleaned
    
    def is_duplicate_telephone(self, telephone: str) -> bool:
        """Check if telephone number already exists in our records"""
        if not telephone:
            return False
        clean_tel = self.clean_telephone(telephone)
        return clean_tel in self.existing_telephones
    
    def add_telephone_to_memory(self, telephone: str):
        """Add telephone number to memory to track duplicates"""
        if telephone:
            clean_tel = self.clean_telephone(telephone)
            if clean_tel:
                self.existing_telephones.add(clean_tel)
    
    def construct_search_query(self, location: Dict[str, Any]) -> str:
        """Construct search query from location data"""
        locality = location.get('locality', '').upper()
        state = location.get('state', '').upper()
        postcode = location.get('postcode', '')
        return f"{locality} {state} {postcode}".strip()
    
    def construct_search_url(self, search_type: str, location_query: str, page: int = 1) -> str:
        """Construct the exact search URL with proper parameters"""
        base_url = f"{BASE_URL}?searchType={search_type}&location={quote_plus(location_query)}&sort=relevance"
        
        if DISTANCE and DISTANCE.strip():
            base_url += f"&distance={DISTANCE}"
            
        # Add services parameter for help-at-home searches
        if search_type == "help-at-home":
            services_encoded = quote_plus("|".join(HELP_AT_HOME_SERVICES))
            base_url += f"&services={services_encoded}"
            
        base_url += f"&page={page}&hasSearched=true"
            
        return base_url

    async def setup_browser(self):
        """Stealth browser setup with all anti-detection techniques"""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",
                "--start-maximized",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor"
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            java_script_enabled=True,
        )
        
        # Add stealth scripts to avoid detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
        """)
        
        return playwright, browser, context

    async def handle_popup(self, page: Page):
        """Handle the 'Got it' popup that appears on the results page"""
        popup_selectors = [
            "button:has-text('Got it')",
            "button:has-text('GOT IT')",
            "[aria-label*='Got it']",
            ".popover button:has-text('Got it')",
            "button >> text=Got it"
        ]
        
        for selector in popup_selectors:
            try:
                popup_button = page.locator(selector)
                if await popup_button.count() > 0 and await popup_button.is_visible():
                    print("Found 'Got it' popup - closing it...")
                    await popup_button.click()
                    await page.wait_for_timeout(1500)
                    print("Popup closed successfully")
                    return True
            except Exception as e:
                continue
        
        return False

    async def goto_with_retry(self, page: Page, url: str):
        """Retry navigation with exponential backoff"""
        for attempt in range(NAV_RETRIES):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                
                # Handle any popups that appear
                await self.handle_popup(page)
                
                return True
                    
            except Exception as e:
                print(f"Navigation attempt {attempt + 1} failed: {str(e)}")
                if attempt == NAV_RETRIES - 1:
                    raise e
                await page.wait_for_timeout(1000 * (attempt + 1))
        return False

    async def check_no_results(self, page: Page) -> bool:
        """Check if the page shows no results found"""
        no_results_indicators = [
            "text=No providers found",
            "text=No results found",
            "text=Try adjusting your search",
            "text=We couldn't find any providers",
            "text=0 results",
            "[data-testid*='no-results']",
            ".no-results",
            "[class*='no-results']"
        ]
        
        for indicator in no_results_indicators:
            try:
                element = page.locator(indicator)
                if await element.count() > 0 and await element.is_visible():
                    print("🔍 No results found for this search")
                    return True
            except:
                continue
        
        # Also check if we're on a search form page instead of results
        current_url = page.url
        if "/find-a-provider/search" in current_url and "results" not in current_url:
            print("🔍 Redirected to search form page - no results")
            return True
            
        return False

    async def wait_for_results(self, page: Page) -> bool:
        """Flexible waiting for results with multiple detection strategies"""
        await self.handle_popup(page)
        
        # First check if there are no results
        if await self.check_no_results(page):
            return False
        
        selectors = [
            "article",
            "li[role='article']",
            "[data-testid*='result']",
            "[class*='result']",
            "[class*='card']",
            "h2",
        ]
        
        for attempt in range(30):
            await self.handle_popup(page)
            
            # Check again for no results
            if await self.check_no_results(page):
                return False
            
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    count = await locator.count()
                    if count > 0:
                        if count >= 1 and await locator.first.is_visible():
                            print(f"Found {count} result elements")
                            return True
                except Exception as e:
                    continue
            await page.wait_for_timeout(500)
        
        # Final check for no results
        if await self.check_no_results(page):
            return False
            
        print("❓ Could not determine if results exist")
        return False

    async def extract_all_links_from_cards(self, page: Page) -> List[str]:
        """Extract all provider detail links from result cards"""
        print("🔍 Extracting links from result cards...")
        
        links = []
        
        # First, let's find all the card containers
        card_selectors = [
            "div.flex.w-full.content-center.bg-neutral-00",  # The main card container from your HTML
            "article",
            "li[role='article']",
            "[data-testid*='result']",
            "[class*='card']",
            "div[class*='flex'][class*='w-full'][class*='bg-neutral-00']"
        ]
        
        for card_selector in card_selectors:
            try:
                cards = page.locator(card_selector)
                card_count = await cards.count()
                print(f"Found {card_count} cards with selector: {card_selector}")
                
                if card_count > 0:
                    for i in range(card_count):
                        try:
                            card = cards.nth(i)
                            
                            # Look for links within each card - multiple strategies
                            link_selectors = [
                                "a[href*='/find-a-provider/']",
                                "a[href*='search/']",
                                "a:has-text('Show details')",
                                "h3 a",  # The company name link
                                "a"  # Any link as fallback
                            ]
                            
                            for link_selector in link_selectors:
                                try:
                                    link_elements = card.locator(link_selector)
                                    link_count = await link_elements.count()
                                    
                                    if link_count > 0:
                                        for j in range(link_count):
                                            try:
                                                href = await link_elements.nth(j).get_attribute("href")
                                                if href:
                                                    # Convert to full URL if relative
                                                    full_url = href if href.startswith('http') else f"https://www.myagedcare.gov.au{href}"
                                                    
                                                    # Filter for actual provider detail pages
                                                    if '/find-a-provider/' in full_url and 'results' not in full_url and full_url not in links:
                                                        links.append(full_url)
                                                        print(f"  📎 Link {len(links)}: {full_url}")
                                                        break  # Found a link for this card, move to next card
                                            except:
                                                continue
                                            
                                    if links and len(links) > i:  # If we found a link for this card
                                        break
                                        
                                except:
                                    continue
                                    
                        except Exception as e:
                            print(f"Error processing card {i}: {e}")
                            continue
                            
            except Exception as e:
                continue
        
        # If we still haven't found links, try a broader search but filter out search results pages
        if not links:
            print("Trying broader link search...")
            all_links = page.locator("a[href*='/find-a-provider/']")
            link_count = await all_links.count()
            print(f"Found {link_count} provider links on page")
            
            for i in range(link_count):
                try:
                    href = await all_links.nth(i).get_attribute("href")
                    if href:
                        full_url = href if href.startswith('http') else f"https://www.myagedcare.gov.au{href}"
                        # Only include actual provider pages, not search results
                        if '/find-a-provider/' in full_url and 'results' not in full_url and full_url not in links:
                            links.append(full_url)
                            print(f"  📎 Link {len(links)}: {full_url}")
                except:
                    continue
        
        # Filter out any search results pages that might have slipped through
        links = [link for link in links if 'results' not in link]
        
        # Apply LINK_PER_SEARCH limit
        if LINK_PER_SEARCH and LINK_PER_SEARCH > 0:
            original_count = len(links)
            links = links[:LINK_PER_SEARCH]
            print(f"🔢 Limited links from {original_count} to {len(links)} based on LINK_PER_SEARCH setting")
        
        print(f"✅ Total links extracted: {len(links)}")
        return links

    async def scrape_detail_page(self, page: Page, url: str, search_type: str, search_location: str) -> Optional[ProviderData]:
        """Scrape all text and parse specific fields from detail page"""
        print(f"🌐 Navigating to: {url}")
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)
            
            # Check if page is valid (not 404 or search page)
            page_title = await page.title()
            current_url = page.url
            
            # Skip if this is a search results page or form page
            if 'results' in current_url or '/find-a-provider/search' in current_url and '/find-a-provider/search/' not in current_url:
                print(f"❌ Skipping search page: {url}")
                return None
            
            if "Sorry, we can't find" in page_title or "404" in page_title or "Page not found" in await page.content():
                print(f"❌ Broken link (404 page): {url}")
                return None
            
            # Extract ALL text from the page
            raw_text = await self.extract_all_text_from_page(page)
            
            # Check if we got meaningful content
            if len(raw_text.strip()) < 100 or "Sorry, we can't find" in raw_text:
                print(f"❌ Page has no meaningful content: {url}")
                return None
            
            # Check if this is actually a provider detail page (not a search form)
            if "Find aged care providers to support your needs" in raw_text:
                print(f"❌ Skipping search form page: {url}")
                return None
            
            # Parse specific fields from the raw text
            company_name = await self._extract_company_name_from_elements(page)
            telephone = self._extract_telephone(raw_text)
            email = self._extract_email(raw_text)
            website = self._extract_website(raw_text)
            address = self._extract_address(raw_text)
            
            # If company name is still empty, try from raw text
            if not company_name:
                company_name = self._extract_company_name_from_text(raw_text)
            
            # Skip if this doesn't look like a real provider page
            if not company_name or company_name == "Find aged care providers to support your needs":
                print(f"❌ Not a valid provider page: {url}")
                return None
            
            # Check for duplicate telephone number
            if telephone and self.is_duplicate_telephone(telephone):
                print(f"🔄 Skipping duplicate telephone number: {telephone}")
                return None
            
            # Extract suburb, state, postcode from search location
            suburb, state, postcode = self._parse_search_location(search_location)
            
            provider_data = ProviderData(
                company_name=company_name,
                address=address,
                suburb=suburb,
                state=state,
                postcode=postcode,
                telephone=telephone,
                email=email,
                website=website,
                search_type=search_type,
                search_location=search_location,
                result_url=url
            )
            
            # Print to terminal
            print("\n" + "="*80)
            print(f"CONTENT FROM: {url}")
            print("="*80)
            print(raw_text[:1500] + "..." if len(raw_text) > 1500 else raw_text)
            print("="*80)
            print(f"EXTRACTED DATA:")
            print(f"Company: {company_name}")
            print(f"Address: {address}")
            print(f"Suburb: {suburb}, State: {state}, Postcode: {postcode}")
            print(f"Telephone: {telephone}")
            print(f"Email: {email}")
            print(f"Website: {website}")
            print(f"Search Type: {search_type}")
            print(f"Search Location: {search_location}")
            print("="*80 + "\n")
            
            return provider_data
            
        except Exception as e:
            print(f"❌ Failed to scrape {url}: {str(e)}")
            return None

    async def extract_all_text_from_page(self, page: Page) -> str:
        """Extract all visible text from the page"""
        try:
            # Get main content area or fallback to body
            content_selectors = [
                "main",
                "[role='main']",
                ".main-content",
                "article",
                ".content",
                "#content",
                ".provider-details",
                "[data-testid*='details']",
                "body"
            ]
            
            for selector in content_selectors:
                try:
                    element = page.locator(selector)
                    if await element.count() > 0:
                        text = await element.inner_text()
                        if text and len(text.strip()) > 100:
                            return text.strip()
                except:
                    continue
            
            # Final fallback to body
            body_text = await page.locator("body").inner_text()
            return body_text.strip()
            
        except Exception as e:
            return f"Error extracting text: {str(e)}"

    async def _extract_company_name_from_elements(self, page: Page) -> str:
        """Extract company name from page elements (more reliable)"""
        # Try multiple selectors for company name
        name_selectors = [
            "h1",
            "h1[data-testid*='name']",
            ".provider-name",
            "header h1",
            "[data-testid*='provider-name']",
            "h2:first-of-type"
        ]
        
        for selector in name_selectors:
            try:
                element = page.locator(selector).first
                if await element.count() > 0:
                    text = await element.inner_text()
                    if text and len(text.strip()) > 0:
                        # Clean up the text - take only the first line if multiple lines
                        clean_text = text.strip().split('\n')[0]
                        if len(clean_text) < 100:  # Reasonable company name length
                            return clean_text
            except:
                continue
        
        return ""

    def _extract_company_name_from_text(self, text: str) -> str:
        """Extract company name from raw text as fallback"""
        # Look for the main heading pattern
        lines = text.split('\n')
        for i, line in enumerate(lines):
            line = line.strip()
            if len(line) > 3 and len(line) < 100:  # Reasonable name length
                # Check if this looks like a company name (not a menu item, etc.)
                if (line.isupper() or any(word[0].isupper() for word in line.split() if word)) and \
                   not any(keyword in line.lower() for keyword in ['home', 'find a provider', 'search', 'print', 'share']):
                    return line
        return ""

    def _extract_telephone(self, text: str) -> str:
        """Extract telephone number from text"""
        # Australian phone number patterns
        patterns = [
            r'\b\d{2} \d{4} \d{4}\b',  # 02 8388 8000
            r'\b\d{4} \d{3} \d{3}\b',  # 1800 864 846
            r'\b\d{2}-\d{4}-\d{4}\b',  # 02-8388-8000
            r'\b\d{4}-\d{3}-\d{3}\b',  # 1800-864-846
            r'\b\d{8}\b',  # 0283888000
            r'\b\d{10}\b',  # 021800864846
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            if matches:
                # Return the first match that looks like a phone number
                for match in matches:
                    if not match.startswith('2025') and not match.startswith('2024'):  # Avoid dates
                        return match.strip()
        
        return ""

    def _extract_email(self, text: str) -> str:
        """Extract email from text"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        match = re.search(email_pattern, text)
        if match:
            return match.group(0).strip()
        return ""

    def _extract_website(self, text: str) -> str:
        """Extract website from text"""
        website_pattern = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*\??[/\w\.-=&%]*'
        matches = re.findall(website_pattern, text)
        if matches:
            # Filter out myagedcare links and return external websites
            for match in matches:
                if 'myagedcare.gov.au' not in match and 'bot.sannysoft.com' not in match:
                    return match.strip()
        return ""

    def _extract_address(self, text: str) -> str:
        """Extract address from text"""
        # Look for address patterns like "1 Cranbrook Road, ROSE BAY 2029 NSW"
        address_pattern = r'(\d+[\sA-Za-z]+,?\s+[A-Z\s]+\s+\d{4}\s+(?:ACT|NSW|NT|QLD|SA|TAS|VIC|WA))'
        match = re.search(address_pattern, text.upper())
        if match:
            return match.group(1).strip()
        
        # Alternative pattern for addresses without commas
        address_pattern2 = r'(\d+[\sA-Za-z]+\s+[A-Z\s]+\s+\d{4}\s+(?:ACT|NSW|NT|QLD|SA|TAS|VIC|WA))'
        match = re.search(address_pattern2, text.upper())
        if match:
            return match.group(1).strip()
        
        return ""

    def _parse_search_location(self, search_location: str) -> tuple[str, str, str]:
        """Parse suburb, state, and postcode from search location"""
        parts = search_location.upper().split()
        if len(parts) >= 3:
            suburb = parts[0]  # "SYDNEY"
            state = parts[1]   # "NSW" 
            postcode = parts[2] # "2000"
            return suburb, state, postcode
        return "", "", ""

    async def run(self):
        """Main scraping workflow with three-level iteration"""
        if not self.locations:
            print("❌ No locations to process. Exiting.")
            return
            
        playwright, browser, context = await self.setup_browser()
        
        try:
            page = await context.new_page()
            
            total_locations = len(self.locations)
            total_search_types = len(SEARCH_TYPES)
            
            print(f"🚀 Starting scraping for {total_locations} locations and {total_search_types} search types")
            print(f"📊 Total iterations: {total_locations * total_search_types}")
            
            # First level iteration: Locations
            for location_index, location in enumerate(self.locations, 1):
                search_query = self.construct_search_query(location)
                print(f"\n{'='*80}")
                print(f"📍 LOCATION {location_index}/{total_locations}: {search_query}")
                print(f"{'='*80}")
                
                # Second level iteration: Search types
                for search_type_index, search_type in enumerate(SEARCH_TYPES, 1):
                    print(f"\n🔍 2nd level @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@SSEARCH TYPE {search_type_index}/{total_search_types}: {search_type}")
                    
                    # Construct search URL for this location and search type
                    search_url = self.construct_search_url(search_type, search_query)
                    print(f"🌐 Search URL: {search_url}")
                    
                    try:
                        # Navigate to search results
                        await self.goto_with_retry(page, search_url)
                        
                        # Wait for results to load and check if there are any results
                        has_results = await self.wait_for_results(page)
                        
                        if not has_results:
                            print(f"🔍 No results found for {search_type} in {search_query} - skipping")
                            continue
                        
                        # Third level iteration: Extract and scrape detail links
                        detail_links = await self.extract_all_links_from_cards(page)
                        
                        if not detail_links:
                            print(f"❌ No valid provider links found for {search_type} in {search_query}")
                            continue
                        
                        print(f"🎯 Found {len(detail_links)} detail pages to scrape")
                        
                        # Scrape each detail page
                        successful_scrapes = 0
                        for i, link in enumerate(detail_links, 1):
                            print(f"\n📖 3rd level ############################################ Scraping page {i} of {len(detail_links)}")
                            
                            provider_data = await self.scrape_detail_page(
                                page, link, search_type, search_query
                            )
                            
                            if provider_data:
                                # Add telephone to memory to track duplicates
                                self.add_telephone_to_memory(provider_data.telephone)
                                self.results.append(asdict(provider_data))
                                successful_scrapes += 1
                                print(f"✅ Successfully scraped: {provider_data.company_name}")
                            else:
                                print(f"❌ Failed to scrape or invalid page: {link}")
                            
                            # Brief pause between requests
                            await page.wait_for_timeout(1000)
                        
                        print(f"✅ Completed {search_type} for {search_query}: {successful_scrapes}/{len(detail_links)} successful")
                            
                    except Exception as e:
                        print(f"💥 Error processing {search_type} for {search_query}: {str(e)}")
                        continue
                
                # Save progress after each location
                self.save_to_csv()
                print(f"💾 Progress saved after location {location_index}/{total_locations}")
            
            print(f"\n🎉 Scraping completed! Total new records: {len(self.results)}")
            
        except Exception as e:
            print(f"💥 Scraping failed: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            await browser.close()
            await playwright.stop()

    def save_to_csv(self):
        """Save results to CSV file (appending if file exists, excluding result_url)"""
        if not self.results:
            print("No new data to save")
            return
            
        # Define fieldnames excluding result_url
        fieldnames = [
            "company_name", "address", "suburb", "state", "postcode", 
            "telephone", "email", "website"
        ]
        
        file_exists = os.path.exists('output/output.csv')
        
        with open('output/output.csv', 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            # Write header only if file doesn't exist
            if not file_exists:
                writer.writeheader()
            
            new_records_count = 0
            for row in self.results:
                # Create a new row without result_url
                csv_row = {}
                for field in fieldnames:
                    csv_row[field] = row.get(field, "")
                
                # Clean company name - remove extra text
                company_name = csv_row['company_name']
                if company_name and '\n' in company_name:
                    company_name = company_name.split('\n')[0].strip()
                csv_row['company_name'] = company_name
                
                writer.writerow(csv_row)
                new_records_count += 1
        
        mode = "Appended to" if file_exists else "Created new"
        print(f"✅ {mode} output.csv with {new_records_count} new records (total unique telephones: {len(self.existing_telephones)})")
        
        # Clear results after saving to avoid duplicates on next save
        self.results.clear()

async def main():
    scraper = MyAgedCareScraper()
    await scraper.run()

if __name__ == "__main__":
    asyncio.run(main())