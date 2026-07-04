import requests
import urllib3
from datetime import datetime, timedelta, timezone
import os
import re
from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import smtplib
from email.message import EmailMessage

# Suppress warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration ---
TOKEN = os.environ.get('READWISE_TOKEN')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
APP_PASSWORD = os.environ.get('APP_PASSWORD')
KINDLE_EMAIL = os.environ.get('KINDLE_EMAIL')

URL = 'https://readwise.io/api/v3/list/'
HEADERS = {'Authorization': f'Token {TOKEN}'}

def fetch_weekly_articles():
    print("Fetching saved articles from the last 7 days...")
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    resp = requests.get(URL, headers=HEADERS, params={'updated__gt': seven_days_ago}, verify=False)
    
    if resp.status_code != 200:
        print(f"API Error: {resp.status_code}")
        return []
    
    articles = resp.json().get('results', [])
    matched_articles = []
    
    print("\n--- Processing Weekly Articles ---")
    for art in articles:
        if art.get('category') == 'article':
            title = art.get('title', 'Unknown Title')
            print(f" -> Fetching full HTML for: {title}")
            
            # Pass 2: Fetch the full payload using the document ID
            detail = requests.get(URL, headers=HEADERS, params={'id': art['id'], 'withHtmlContent': 'true'}, verify=False)
            if detail.status_code == 200:
                full_data = detail.json().get('results', [{}])[0]
                art['html_content'] = full_data.get('html_content') or full_data.get('summary') or "No content available."
                art['image_url'] = full_data.get('image_url') or art.get('image_url')
                matched_articles.append(art)
                
    return matched_articles

def generate_cover(articles):
    print("\nDrawing custom cover with article artwork...")
    img = Image.new('RGB', (600, 800), color=(244, 244, 245))
    d = ImageDraw.Draw(img)
    header_img = None
    
    for art in articles:
        img_url = art.get('image_url')
        if img_url and img_url.startswith('http'):
            try:
                print(f" -> Downloading cover art from: {art.get('title')}")
                img_resp = requests.get(img_url, timeout=10)
                if img_resp.status_code == 200:
                    downloaded_img = Image.open(BytesIO(img_resp.content)).convert('RGB')
                    target_width, target_height = 600, 350
                    img_ratio = downloaded_img.width / downloaded_img.height
                    target_ratio = target_width / target_height
                    
                    if img_ratio > target_ratio:
                        new_width = int(target_height * img_ratio)
                        resized = downloaded_img.resize((new_width, target_height), Image.Resampling.LANCZOS)
                        left = (new_width - target_width) / 2
                        header_img = resized.crop((left, 0, left + target_width, target_height))
                    else:
                        new_height = int(target_width / img_ratio)
                        resized = downloaded_img.resize((target_width, new_height), Image.Resampling.LANCZOS)
                        top = (new_height - target_height) / 2
                        header_img = resized.crop((0, top, target_width, top + target_height))
                    break
            except Exception as e:
                print(f" -> Could not load image: {e}")
                continue
                
    if header_img:
        # Convert cover to Grayscale to save space
        header_img = header_img.convert('L')
        img.paste(header_img, (0, 0))
        y_offset = 390
    else:
        y_offset = 40
        
    try:
        font_title = ImageFont.truetype("Impact.ttf", 46)
        font_date = ImageFont.truetype("Arial.ttf", 20)
        font_list = ImageFont.truetype("Arial.ttf", 16)
    except IOError:
        font_title = font_date = font_list = ImageFont.load_default()
        
    d.text((40, y_offset), "WEEKLY ARTICLE DIGEST", fill=(17, 17, 17), font=font_title)
    d.text((40, y_offset + 60), datetime.now().strftime("%A, %B %d, %Y").upper(), fill=(100, 100, 100), font=font_date)
    d.line([(40, y_offset + 95), (560, y_offset + 95)], fill=(0, 0, 0), width=3)
    
    y_text = y_offset + 125
    for art in articles[:8]:
        raw_title = art.get('title', 'Untitled')
        clean_title = raw_title.split(' | ')[0].split(' - ')[0].strip()
        if len(clean_title) > 42:
            clean_title = clean_title[:39] + "..."
        d.text((40, y_text), f"• {clean_title}", fill=(50, 50, 50), font=font_list)
        y_text += 30
        
    # Compress cover aggressively
    img.save('weekly_cover.jpg', optimize=True, quality=60)

def package_to_epub(articles):
    print("Stitching text, embedding images, and building EPUB...")
    book = epub.EpubBook()
    book.set_identifier('weekly_readwise_digest')
    book.set_title(f"Weekly Digest - {datetime.now().strftime('%b %d, %Y')}")
    book.set_language('en')
    
    # Attach the Custom Cover
    generate_cover(articles)
    with open('weekly_cover.jpg', 'rb') as cover_file:
        book.set_cover("cover.jpg", cover_file.read())
        
    toc_html = "<h1>Table of Contents</h1><ul>"
    chapters = []
    image_counter = 0
    
    for i, art in enumerate(articles):
        title = art.get('title', 'Untitled')
        author = art.get('author', 'Unknown Author')
        content = art.get('html_content', '')
        
        # Add to the TOC list
        toc_html += f'<li><a href="chap_{i}.xhtml">{title}</a></li>'
        
        # Nuke hidden Base64 image data that bloats the HTML text
        content = re.sub(r'src="data:image/[^"]+"', 'src=""', content, flags=re.IGNORECASE)
        
        # Find and embed external images, converting to B&W
        img_urls = re.findall(r'src="([^"]+)"', content, re.IGNORECASE)
        for img_url in set(img_urls):
            if not img_url.startswith('http'):
                continue
            try:
                img_resp = requests.get(img_url, timeout=5)
                if img_resp.status_code == 200:
                    img_obj = Image.open(BytesIO(img_resp.content))
                    
                    # Convert to Grayscale ('L' mode) to obliterate file size
                    img_obj = img_obj.convert('L')
                    
                    # Shrink large images to fit Kindle width
                    max_width = 600
                    if img_obj.width > max_width:
                        ratio = max_width / img_obj.width
                        new_h = int(img_obj.height * ratio)
                        img_obj = img_obj.resize((max_width, new_h), Image.Resampling.LANCZOS)
                        
                    # Compress the image data aggressively
                    output_io = BytesIO()
                    img_obj.save(output_io, format='JPEG', quality=60, optimize=True)
                    compressed_content = output_io.getvalue()
                    
                    img_name = f"embedded_img_{image_counter}.jpg"
                    img_item = epub.EpubItem(
                        uid=img_name, 
                        file_name=f"images/{img_name}", 
                        media_type="image/jpeg", 
                        content=compressed_content
                    )
                    book.add_item(img_item)
                    content = content.replace(img_url, f"images/{img_name}")
                    image_counter += 1
            except Exception:
                pass 
                
        # Build the chapter
        c = epub.EpubHtml(title=title, file_name=f'chap_{i}.xhtml', lang='en')
        c.content = f"<h2>{title}</h2><p><b>By {author}</b></p>{content}"
        book.add_item(c)
        chapters.append(c)
        
    toc_html += "</ul>"
    toc_chapter = epub.EpubHtml(title='Table of Contents', file_name='toc.xhtml', lang='en')
    toc_chapter.content = toc_html
    book.add_item(toc_chapter)
    
    book.spine = [toc_chapter] + chapters
    epub.write_epub('weekly_digest.epub', book, {})
    
    if os.path.exists('weekly_cover.jpg'):
        os.remove('weekly_cover.jpg')

def send_to_kindle():
    print(f"\nPreparing to deliver 'weekly_digest.epub' to {KINDLE_EMAIL}...")
    
    # Log the final file size to the terminal
    file_size_mb = os.path.getsize('weekly_digest.epub') / (1024 * 1024)
    print(f"Final EPUB Size: {file_size_mb:.2f} MB")
    
    if file_size_mb > 24.0:
        print("WARNING: File size is dangerously close to or exceeding Google's 25MB limit!")
        
    msg = EmailMessage()
    msg['Subject'] = 'Convert'
    msg['From'] = SENDER_EMAIL
    msg['To'] = KINDLE_EMAIL
    
    try:
        with open('weekly_digest.epub', 'rb') as f:
            file_data = f.read()
        msg.add_attachment(file_data, maintype='application', subtype='epub+zip', filename='weekly_digest.epub')
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(SENDER_EMAIL, APP_PASSWORD)
            smtp.send_message(msg)
        print("Delivery Successful! The digest is on its way to your Kindle.")
    except Exception as e:
        print(f"Delivery Failed: {e}")

if __name__ == '__main__':
    articles = fetch_weekly_articles()
    if articles:
        package_to_epub(articles)
        send_to_kindle()
    else:
        print("\nNo articles found to package.")
