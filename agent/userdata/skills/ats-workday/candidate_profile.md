---
# CANDIDATE PROFILE — single source of truth for ALL Workday form fields.
# This file is co-located with the ats-workday skill. The agent MUST read it
# before applying and pull every answer from here. DO NOT ask the user for any
# field that appears in this file. DO NOT invent data not present here.
# Last confirmed: 2026-07-19 (Evgeny Vainshtein).
---

# Candidate Profile — Evgeny Vainshtein

## identity
- full_name: Evgeny Vainshtein
- first_name: Evgeny
- last_name: Vainshtein
- gender: Male            # self-disclosure; required on Voluntary Disclosures despite "optional" label
- linkedin: https://www.linkedin.com/in/feranicus/

## contact
- email: evgeny@s4biz.io
- phone: "+49 15785541545"
- phone_type: Mobile
- country: Germany
- address_line: Hermann-J.-Bach-Weg 16
- city: Friedberg
- state_region: Hessen
- postal_code: "61169"
- land_bundesland: ""     # optional — leave blank

## workday_account
- email: evgeny@s4biz.io
- password_env: HERMES_ATS_WORKDAY_PASSWORD   # stored secret; reuse on every Workday apply
- former_ge_vernova_employee: "No"
- how_did_you_hear: LinkedIn   # select under "Job Board" cascade

## resume
- path: C:\Users\feran\Downloads\Resumes and positions\jevpreDE2025_updated.pdf
- note: ATS-friendly PDF (reportlab). Contains current Colt 2026 role + ZEDEDA certs.

## education
- edu_1:
    school: Champlain College
    degree: "Bachelor of Science (B.S.)"
    field_of_study: Computer Science
    from_year: 2000
    to_year: 2003
    location: Burlington, VT, USA
- edu_2:
    school: The ORT Ashdod Technological School for Naval Officers
    degree: Diploma
    field_of_study: "Navy Officer Practical Engineering (Marine Electricity)"
    from_year: 1995
    to_year: 2000
    location: Ashdod, Israel
- cert_1:
    name: AWS Certified Cloud Practitioner
    issuer: Amazon Web Services
    from_year: 2021
    to_year: 2022

## work_experience
- work_1:
    title: "Principal Technical Architect — Cyber, Cloud, Digital Transformation & AI (Presales Director)"
    company: Stars4Business OÜ
    currently_work_here: true
    from_month: 1
    from_year: 2016
    to_month: ""
    to_year: ""
    description: >-
      Senior T&T director-level freelance across DACH — multi-million cloud
      transformations (AWS/Azure/GCP), DevSecOps, zero-trust security,
      Kubernetes (EKS/AKS), and offensive-cyber programs.
- work_2:
    title: "Cyber Security Client Partner"
    company: Colt Technology Services
    currently_work_here: true
    from_month: 2
    from_year: 2026
    to_month: ""
    to_year: ""
    description: >-
      Client partner for cyber-security solutions across DACH enterprise
      accounts (per LinkedIn export, Feb 2026–Present).
- work_earlier_note: >-
    Earlier roles at Telefónica / AWS, RedHat, NetApp, ENEA, Luxair — NO clean
    dates available; do NOT invent. Omit from structured form fields unless the
    candidate supplies dates; the uploaded CV covers them.

## skills
- Zero Trust / SASE / SD-WAN
- SIEM / XDR / MDR
- IAM / PAM
- CSPM / CWPP
- DevSecOps
- AWS / Azure / GCP
- Kubernetes (EKS / AKS)
- Terraform / Ansible
- Digital Transformation
- Program & Transition & Transformation Management
- MEDDPICC
- GDPR / DORA / NIS2
- Top Skills (LinkedIn): Cybersecurity, Linux, Kubernetes

## application_questions (Workday screening — match by question substring)
- non_competition_agreement: "No"
- confidential_proprietary_info: "Yes, I agree."
- legally_authorized_to_work: "Yes"
- former_government_employee: "No"
- immediate_family_government: "No"   # TRUTHFUL. Answering "Yes" spawns an extra required question — avoid.
- communication_method: "Email"
- consent_terms: CHECKED

## notes
- Never submit bad data. At Review, verify every field against this file.
- Auto-submit at Review is ENABLED (user override 2026-07-19). Only PAUSE for
  email-verification during account creation.
