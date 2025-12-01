# 🚀 JobHuntWOW.com  
### Automated, KISS-Driven Job Interview Tracker (Open Source)

JobHuntWOW.com is a fully automated job interview tracking platform powered by  
Gmail → Google Apps Script → Google Sheets → React Dashboard.

Built for high-performing professionals managing **5–20 active interview pipelines**, drowning in recruiter emails, missing follow-ups, or losing track of next steps — simply because Gmail is chaos.

This project turns your job search into a WOW experience:  
Zero backend. Zero manual input. 100% automation. 100% KISS.

---

## 🌟 Features

### 📥 Gmail Auto-Parsing  
- Automatically scans your Gmail inbox  
- Detects interview-related emails using advanced keyword + RegExp matching  
- Extracts:  
  - Company  
  - Role  
  - Interview date/time  
  - Hiring stage  
  - HR contact  
  - Last message sentiment  
  - Status & next steps  
- Updates automatically using triggers  

### 🧠 Google Apps Script Automation  
- Converts email chaos → structured data  
- Writes directly into Google Sheets  
- Auto-detects stages (HR, Tech, Director, Final, Offer)  
- Generates follow-up dates  
- CRON updates every 15 minutes  
- No servers or maintenance required  

### 📊 Google Sheets as a Database  
Clean, transparent, fully exportable.

Columns provided:  
| ID | Company | Role | Stage | Next Interview | Last Email | HR Contact | Status | Next Follow-up | Notes |

### 🌐 React Dashboard  
A beautiful, real-time UI powered by Sheets API:

- Kanban pipeline  
- Interview timeline  
- Real-time analytics  
- Charts (conversion rate, interviews/week, ghosting stats)  
- Company cards  
- Status auto-coloring  
- Search & filtering  

### 🔔 Optional Notifications  
- Telegram bot  
- Slack alerts  
- Email reminders  
- Interview countdown  

---

## 🏗️ Architecture (Cloud-native, KISS)

.