#!/usr/bin/env python3
"""
Workday last-3-days watcher (GitHub Actions edition).

What changed from the desktop version:
- PROJECT_DIR is now relative to this file (repo-safe), not a Windows path.
- All timestamps are EST/EDT-aware (America/New_York), so "today" means
  today in New York regardless of what timezone the CI runner uses.
- jobs_db.json is the persistence layer. It lives in data/ and is committed
  back to the repo by the GitHub Actions workflow after every run. This is
  what makes "NEW!" tagging possible across runs on a stateless runner.
- Daily output file: data/openings_MMDDYYYY.md. One file per EST calendar
  day. The file is fully regenerated each run from jobs_db.json (not
  appended to), filtered to jobs whose first_seen date == today. This
  avoids duplicate rows and drift. Jobs discovered in THIS run get a
  "(NEW!)" tag; jobs discovered in an earlier run today do not.
- Everything else (facets, US-only filtering, company list) is unchanged
  from your original script.

Run:
    python workday_last3days_watcher.py

Optional:
    pip install requests pandas
"""

import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from functools import partial
from zoneinfo import ZoneInfo

import requests

try:
    import pandas as pd
except Exception:
    pd = None

print = partial(print, flush=True)

EST = ZoneInfo("America/New_York")


def now_est():
    return datetime.now(EST)


# ============ CONFIG ============
# Repo-relative. This file sits at repo root; data/ is a sibling folder.
PROJECT_DIR = Path(__file__).resolve().parent / "data"
PROJECT_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE      = PROJECT_DIR / "jobs_db.json"
RUN_FILE     = PROJECT_DIR / "last_run.json"
PRIORITY_MD  = PROJECT_DIR / "priority.md"
PRIORITY_CSV = PROJECT_DIR / "priority.csv"
LAST3_CSV    = PROJECT_DIR / "last3days.csv"
ALL_RAW_CSV  = PROJECT_DIR / "raw_scraped_jobs.csv"
ERRORS_JSONL = PROJECT_DIR / "scrape_errors.jsonl"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; WorkdayWatcher/1.0)",
}

PAGE = 20
MAX_DAYS = 3
PRIORITY_DAYS = 1
SLEEP = 0.25
US_ONLY = True  # set False if you want literally all countries
KEEP_AMBIGUOUS_LOCATIONS = True  # keep '2 Locations', '4 Locations', 'Multiple Locations'
USE_CATEGORY_FACETS = False  # False = truly all jobs; True = only discovered tech/data/product categories
MAX_OFFSET = 3000

# ============ FACETED COMPANIES ============
COMPANIES = [
    ('Walmart', 'walmart.wd504.myworkdayjobs.com', 'walmart', 'walmartexternal', {"jobFamilyGroup": ["e83ebdbd2a0a01e7e1477a8948e904c6", "e83ebdbd2a0a01af0185848948e94dc6", "e83ebdbd2a0a01050ff47e8948e912c6", "e83ebdbd2a0a01ea72c2808948e924c6"]}),
    ('Accenture', 'accenture.wd103.myworkdayjobs.com', 'accenture', 'AccentureCareers', {"jobFamilyGroup": ["bb69a804fb120130e52200ed1301d275", "99f04fef1b5710010b161504c3910000", "bb69a804fb1201a1606f05ed1301dc75", "bb69a804fb1201712ed6f3ec1301bc75", "bb69a804fb1201653003f3ec1301ba75"]}),
    ('Intel', 'intel.wd1.myworkdayjobs.com', 'intel', 'External', {"jobFamilyGroup": ["c37a9eaa90371000c6fd2261025d0000", "ace7a3d23b7e01a0544279031a0ec85c", "c37a9eaa90371000c6fd29069de10000", "dc8bf79476611087d67b2cccdde47034", "a55ea4dd831d1000c6fce5a0c4d30000"]}),
    ('Qualcomm', 'qualcomm.wd12.myworkdayjobs.com', 'qualcomm', 'External', {}),
    ('Salesforce', 'salesforce.wd12.myworkdayjobs.com', 'salesforce', 'External_Career_Site', {"jobFamilyGroup": ["14fa3452ec7c1011f90d0002a2100000", "14fa3452ec7c1011f90cf2c552640000", "14fa3452ec7c1011f90cf8c9c5960000", "14fa3452ec7c1011f90cf661a7c80000"]}),
    ('NVIDIA', 'nvidia.wd5.myworkdayjobs.com', 'nvidia', 'NVIDIAExternalCareerSite', {"jobFamilyGroup": ["0c40f6bd1d8f10ae43ffaefd46dc7e78", "0c40f6bd1d8f10ae43ffbd1459047e84"]}),
    ('Fidelity', 'fmr.wd1.myworkdayjobs.com', 'fmr', 'FidelityCareers', {"jobFamilyGroup": ["e39fd413f80c0104eb5775256a997b12", "e39fd413f80c01c8934aaa256a998f12", "4c9bbf7088c401011719f359748d0000", "e39fd413f80c0146bcdc1b256a995512", "c69cb9369e1310019f0c2abf50430000"]}),
    ('Citi', 'citi.wd5.myworkdayjobs.com', 'citi', '2', {"jobFamilyGroup": ["e32326e1708d01575bddff0c120102c1", "e32326e1708d01ae8f26fe0c1201fcc0", "e32326e1708d01298898fd0c1201fac0", "538c239234271000c428fd3827220000"]}),
    ('Cisco', 'cisco.wd5.myworkdayjobs.com', 'cisco', 'Cisco_Careers', {"jobFamilyGroup": ["2101eee3ea96016aef42a674fc016429", "2101eee3ea96017b1ceba674fc016829"]}),
    ('Adobe', 'adobe.wd5.myworkdayjobs.com', 'adobe', 'external_experienced', {"jobFamilyGroup": ["591af8b812fa10737af39db3d96eed9f", "591af8b812fa10737b0e880e0e3eeee9"]}),
    ('PayPal', 'paypal.wd1.myworkdayjobs.com', 'paypal', 'jobs', {"jobFamilyGroup": ["b00c2f6141401001c5f81018e4210000", "83d6d96d27f71001c5f643e351300000"]}),
    ('FedEx', 'fedex.wd1.myworkdayjobs.com', 'fedex', 'FXE-LAC_External_Career_Site', {"jobFamilyGroup": ["563426b3fcf4010923be1a87ab289222", "563426b3fcf401434b266687ab28b022", "563426b3fcf401d043ac3487ab289c22"]}),
    ('PwC Experienced', 'pwc.wd3.myworkdayjobs.com', 'pwc', 'Global_Experienced_Careers', {"jobFamilyGroup": ["b38cfc0f829110144280fa7cb7390000", "83dadf5ea2a310144280a2bc558e0000", "fcc2b0980c4d1014427ed0de08fe0000", "64bffa1b0975101442803d391e8d0000", "fc91b97ed3901014427f0c8a1b840000"]}),
    ('PwC Campus', 'pwc.wd3.myworkdayjobs.com', 'pwc', 'Global_Campus_Careers', {"jobFamilyGroup": ["83dadf5ea2a310144280a2bc558e0000", "64bffa1b0975101442803d391e8d0000", "b38cfc0f829110144280fa7cb7390000", "fcc2b0980c4d1014427ed0de08fe0000", "e57e6863118d01e656421646202b7868"]}),
    ('U.S. Bank', 'usbank.wd1.myworkdayjobs.com', 'usbank', 'US_Bank_Careers', {"jobFamilyGroup": ["83f76798575a013b4ceb24cfc90555cb", "83f76798575a012c1fc11acfc9052dcb"]}),
    ('General Motors', 'generalmotors.wd5.myworkdayjobs.com', 'generalmotors', 'Careers_GM', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["81219c91208501dea617b5999c1b01d6", "81219c91208501f8ce07be999c1b21d6", "5ce64061bbf21001e72608a46d250000", "81219c91208501c4eb61bf999c1b25d6", "81219c91208501d97097b7999c1b0bd6"]}),
    ('Bank of America', 'ghr.wd1.myworkdayjobs.com', 'ghr', 'Lateral-US', {}),
    ('Morgan Stanley', 'ms.wd5.myworkdayjobs.com', 'ms', 'External', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["e38d1cf2ff5a10014d19aa66ad230000", "96af745083d401ecd47812d07856070d", "c775545e643010014d19582ead2b0000", "9065a9a95b1a10014d20d69599ed0000", "b46569570f4b10014d292e3ae0a00000"]}),
    ('Capital One', 'capitalone.wd12.myworkdayjobs.com', 'capitalone', 'Capital_One', {"jobFamilyGroup": ["a12c70bf789e105802e9e79458dc29ab", "a12c70bf789e105802e9f44b764529b7"]}),
    ('Comcast', 'comcast.wd5.myworkdayjobs.com', 'comcast', 'Comcast_Careers', {"jobFamilyGroup": ["285386867dd9010665c0d2c57d0b4c15", "285386867dd901a0ab5627c67d0b5815", "285386867dd9012db7e3ddc57d0b4e15"]}),
    ('Wells Fargo', 'wf.wd1.myworkdayjobs.com', 'wf', 'WellsFargoJobs', {"jobFamilyGroup": ["b5c3287c76c20100b318a19542940001", "b5c3287c76c20100b3189b6fdb430000", "b5c3287c76c20100b318a04db6d00001"]}),
    ('Visa', 'visa.wd5.myworkdayjobs.com', 'visa', 'Visa', {"jobFamilyGroup": ["e8c806498390105c6260a580252a0363", "2745bc1368021016adc549cafd9a3ba5", "d48eccdc23121000b8a44df0bd370000"]}),
    ('Visa Early Careers', 'visa.wd5.myworkdayjobs.com', 'visa', 'Visa_Early_Careers', {"jobFamilyGroup": ["e8c806498390105c6260a580252a0363", "2745bc1368021016adc549cafd9a3ba5"]}),
    ('Mastercard', 'mastercard.wd1.myworkdayjobs.com', 'mastercard', 'CorporateCareers', {"jobFamilyGroup": ["189119ebe266100103737c3d6a6e0000", "2008c8ccf9ae4e56b7d0ea7b3d319a98", "866c0ed135ff106f00587685e7483440", "0ea6171d1dd81001035429e2c6b00000", "9290e3c013ea406499c856229ef7803a"]}),
    ('Mastercard Campus', 'mastercard.wd1.myworkdayjobs.com', 'mastercard', 'Campus', {"jobFamilyGroup": ["2008c8ccf9ae4e56b7d0ea7b3d319a98"]}),
    ('FIS', 'fis.wd5.myworkdayjobs.com', 'fis', 'SearchJobs', {"jobFamilyGroup": ["041bdfd5e4d01001834cb559a5f10000", "7f383875dd52100183463a63dbc20000", "a0b9127138f4100183639e55ae1b0000", "d26eff0fee761001831d2319121c0000", "a2088ec533f410263c621eb9a6a450a8"]}),
    ('Fiserv', 'fiserv.wd5.myworkdayjobs.com', 'fiserv', 'EXT', {"jobFamilyGroup": ["c6b68e57f5a3108c4614399740ec75a9", "95a96e85f0f61038722586f805eedbd8", "8f0515f038cd1001a957b5c1ee150000", "c6b68e57f5a3108c3da4397d1eec3825", "2ffe4a0bcac71000cc3f08874a9b0000"]}),
    ('BlackRock', 'blackrock.wd1.myworkdayjobs.com', 'blackrock', 'BlackRock_Professional', {"jobFamilyGroup": ["7c29ce3598461001f7505cfee2240000", "131d7f8fcf9f016333d12bd863508621", "131d7f8fcf9f01d4eb1540d863509421"]}),
    ('Barclays', 'barclays.wd3.myworkdayjobs.com', 'barclays', 'External_Career_Site_Barclays', {"jobFamilyGroup": ["112c054282011001e9162cfccdc10000", "1ab48a98eb7c1001e8e0bdc7d4a10000", "1ab48a98eb7c1001e8e0f1fd6ba70000", "112c054282011001e916114dbeec0000"]}),
    ('State Street', 'statestreet.wd1.myworkdayjobs.com', 'statestreet', 'Global', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["56250981e7cb01b9a0c0c6893041490c", "56250981e7cb01a16257d5893041510c", "d90a3d42ffbd1001e98d01e331230000"]}),
    ('Vanguard', 'vanguard.wd5.myworkdayjobs.com', 'vanguard', 'vanguard_external', {"jobFamilyGroup": ["ab11cd13cf5301dd13c78a2c49011dab", "ece02ef1b34201c18b2b82f4d43eef57", "603b667bc800018459db5f733247d99c"]}),
    ('Truist', 'truist.wd1.myworkdayjobs.com', 'truist', 'Careers', {}),
    ('Charles Schwab', 'schwab.wd1.myworkdayjobs.com', 'schwab', 'External', {}),
    ('AT&T', 'att.wd1.myworkdayjobs.com', 'att', 'ATTGeneral', {"jobFamilyGroup": ["28752101615f1001100706f6adce0000"]}),
    ('Micron', 'micron.wd1.myworkdayjobs.com', 'micron', 'External', {}),
    ('T-Mobile', 'tmobile.wd1.myworkdayjobs.com', 'tmobile', 'External', {}),
    ('HPE', 'hpe.wd5.myworkdayjobs.com', 'hpe', 'Jobsathpe', {"jobFamilyGroup": ["98cbd30d374e10333e00271ec8445e81", "98cbd30d374e10333e000e782ca45e69", "98cbd30d374e10333e000a9ad71c5e65"]}),
    ('HP', 'hp.wd5.myworkdayjobs.com', 'hp', 'ExternalCareerSite', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["98cbd30d374e10333e00271ec8445e81", "80667f5f2da3010ee8417a74cf4b0000", "98cbd30d374e10333e000e782ca45e69", "98cbd30d374e10333e000a9ad71c5e65"]}),
    ('HP Inc', 'hpinc.wd5.myworkdayjobs.com', 'hpinc', 'HPINC', {}),
    ('Expedia', 'expedia.wd108.myworkdayjobs.com', 'expedia', 'search', {"jobFamilyGroup": ["c553432013ba103b60decedc3beb2900"]}),
    ('Applied Materials', 'amat.wd1.myworkdayjobs.com', 'amat', 'External', {"jobFamilyGroup": ["12cd0bd5b8c8100a5ff4c01480fc1e9a", "12cd0bd5b8c8100a5ff4d2734ef41ea8", "46f54a07df811001f35598410dcd0000"]}),
    ('ASML', 'asml.wd3.myworkdayjobs.com', 'asml', 'ASMLEXT1', {"jobFamilyGroup": ["719a7319274f010148486f26e0840000", "719a7319274f01014848718f5b8e0002", "719a7319274f0101484876606da70001", "719a7319274f01014848718f5b8e0000", "719a7319274f01014848752b77760001"]}),
    ('KLA', 'kla.wd1.myworkdayjobs.com', 'kla', 'Search', {"jobFamilyGroup": ["bcb876733f8601b7cce000ff551b9f1e", "bcb876733f8601be109502ff551bab1e", "bcb876733f860139e42801ff551ba11e"]}),
    ('KLA University Recruiting', 'kla.wd1.myworkdayjobs.com', 'kla', 'UR', {"jobFamilyGroup": ["bcb876733f8601b7cce000ff551b9f1e"]}),
    ('Intuitive Surgical', 'intuitive.wd1.myworkdayjobs.com', 'intuitive', 'irtc_careers', {}),
    ('Workday', 'workday.wd5.myworkdayjobs.com', 'workday', 'Workday', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["8c5ce7a1cffb43e0a819c249a49fcb00", "3745527d2b3049a889f9cec4740ae41c", "4b2f970c50930155b9985193020a0c72", "a88cba90a00841e0b750341c541b9d56"]}),
    ('Yahoo', 'ouryahoo.wd5.myworkdayjobs.com', 'ouryahoo', 'careers', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["91f14896cbbe0150163e1d3fc7463fb2", "91f14896cbbe0172a9e4f13ec7462fb2"]}),
    ('NTT Ltd', 'nttlimited.wd3.myworkdayjobs.com', 'nttlimited', 'NTT_Careers', {"jobFamilyGroup": ["b452fc8f835a0101f46af0a0aae20000", "cf462829407701c2079a48eab500773d", "ec3c8e45cd230101ab1458a7250a0000", "cf4628294077014a40eceeebb500923d", "f5727257acae010cab9cd7e5b5001853"]}),
    ('NTT Global Data Centers', 'nttglobaldatacenters.wd501.myworkdayjobs.com', 'nttglobaldatacenters', 'External', {"jobFamilyGroup": ["089cc9a8695a1000c0cece8954a60000", "089cc9a8695a1000c0cf9a67dece0000", "089cc9a8695a1000c0cecfbd232c0000", "089cc9a8695a1000c0cf9e9dc8580000"]}),
    ('Cadence', 'cadence.wd1.myworkdayjobs.com', 'cadence', 'External_Careers', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"]}),
    ('Cadence University Careers', 'cadence.wd1.myworkdayjobs.com', 'cadence', 'Univ_Careers', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"]}),
    ('Analog Devices', 'analogdevices.wd1.myworkdayjobs.com', 'analogdevices', 'External', {"jobFamilyGroup": ["633b03df4f5d1000ec24734fbbbb0000", "633b03df4f5d1000ec247e24e03b0000", "633b03df4f5d1000ec243f6e4cb40001", "d7b302590627100dddeb034354610000", "633b03df4f5d1000ec2458c3865d0000"]}),
    ('Nike', 'nike.wd1.myworkdayjobs.com', 'nike', 'nke', {"jobFamilyGroup": ["cd128a2666561000bad388d2f6030002", "cd128a2666561000bad388380ecd0001", "cd128a2666561000bad382c400280000"]}),
    ('Marvell', 'marvell.wd1.myworkdayjobs.com', 'marvell', 'MarvellCareers', {"jobFamilyGroup": ["65dea26481d0016bd6c014dd98175a01", "65dea26481d0011d9f4e14dd98175801", "65dea26481d001fb93160edd98173001", "65dea26481d00140b63919dd98177001", "65dea26481d001660cf219dd98177401"]}),
    ('Motorola Solutions', 'motorolasolutions.wd5.myworkdayjobs.com', 'motorolasolutions', 'Careers', {"jobFamilyGroup": ["2161bef685534428b91fad96fc9069b4", "c3fc17b768e842e39b7192f0bf4cb0f1"]}),
    ('Eli Lilly', 'lilly.wd115.myworkdayjobs.com', 'lilly', 'LLY', {"jobFamilyGroup": ["99c6e09d03e801e1360b7f7ff04aee33", "99c6e09d03e80198acd7817ff04af833"]}),
    ('Amgen', 'amgen.wd1.myworkdayjobs.com', 'amgen', 'Careers', {"jobFamilyGroup": ["3b16b67900e510859633b621ace7c537", "5d5ff483caeb105761524c3d7a0ca833", "ab0594889cd101dccde30fbea601c66a"]}),
    ('Cigna / Evernorth', 'cigna.wd5.myworkdayjobs.com', 'cigna', 'cignacareers', {"Location_Country": ["bc33aa3152ec42d4995f4791a106ed09"], "jobFamilyGroup": ["b7947bbbfff20176f0ee1016390c8d16", "b7947bbbfff201d41a750216390c8516", "b7947bbbfff201f0c3a02116390c9716"]}),
    ('CVS Health', 'cvshealth.wd1.myworkdayjobs.com', 'cvshealth', 'CVS_Health_Careers', {"jobFamilyGroup": ["e65dbadf6a50100168ed86fe4cf50001", "e65dbadf6a50100168ed7f2a693c0001"]}),
    ('St. Jude', 'stjude.wd1.myworkdayjobs.com', 'stjude', 'stjude', {"jobFamilyGroup": ["54f6135fdf27101b2772968d96a80000"]}),
    ('Medtronic', 'medtronic.wd1.myworkdayjobs.com', 'medtronic', 'MedtronicCareers', {"jobFamilyGroup": ["2fe8588f35e84eb98ef535f4d738f243", "4e8537909ca04133879bbd846eef97bf"]}),
    ('Medline', 'medline.wd5.myworkdayjobs.com', 'medline', 'Medline', {"jobFamilyGroup": ["a71dfaf3f3d3100165b3d8f2aa740000", "a71dfaf3f3d3100165b3e6c390890000"]}),
    ('Elevance Health', 'elevancehealth.wd1.myworkdayjobs.com', 'elevancehealth', 'ANT', {"jobFamilyGroup": ["f42bff05a414010057201f1c4a500000"]}),
    ('GEICO', 'geico.wd1.myworkdayjobs.com', 'geico', 'External', {"jobFamilyGroup": ["da128ce5a1dc103e7c09aaa3fe312266", "a35ed0a458b310010910c29142fd0000"]}),
    ('Humana', 'humana.wd5.myworkdayjobs.com', 'humana', 'Humana_External_Career_Site', {"jobFamilyGroup": ["fbb60995c999011d23ea024a15c58ec6", "fbb60995c99901e15cebc24915c582c6", "fbb60995c99901ebd9a7d74915c586c6", "fbb60995c999015ce05ca94715c51ac6", "fbb60995c999019d8a70164a15c592c6"]}),
    ('CenterWell', 'humana.wd5.myworkdayjobs.com', 'humana', 'CenterWell_External_Career_Site', {"jobFamilyGroup": ["fbb60995c9990180a257d34715c522c6", "fbb60995c999019d8a70164a15c592c6", "fbb60995c99901e47c1c8d4915c578c6", "fbb60995c999011d23ea024a15c58ec6", "fbb60995c99901caeffe834915c576c6"]}),
    ('Mass General Brigham', 'massgeneralbrigham.wd1.myworkdayjobs.com', 'massgeneralbrigham', 'MGBExternal', {"jobFamily": ["1856eb1940d51000cc9eb3df6d780000"]}),
    ('Thermo Fisher', 'thermofisher.wd5.myworkdayjobs.com', 'thermofisher', 'ThermoFisherCareers', {}),
    ('Gilead', 'gilead.wd1.myworkdayjobs.com', 'gilead', 'gileadcareers', {"jobFamilyGroup": ["dda565f4c0a3100e2692aaf4d1de9a48"]}),
    ('Kite Pharma / Gilead', 'gilead.wd1.myworkdayjobs.com', 'gilead', 'kitepharmacareers', {}),
    ('USAA', 'usaa.wd1.myworkdayjobs.com', 'usaa', 'USAAJOBSWD', {"jobFamilyGroup": ["e283bdbc2d1210875c9178c49ddb0bf3", "e283bdbc2d1210875c917eefd15f0bf7"]}),
    ('Bristol Myers Squibb', 'bristolmyerssquibb.wd5.myworkdayjobs.com', 'bristolmyerssquibb', 'BMS', {"jobFamilyGroup": ["149748d319111024cd21c66e420c40ae", "8c6312fcafa501cdcaee3314fb01430e", "149748d319111024cd21b32afd1440a2"]}),
    ('WashU St. Louis', 'wustl.wd1.myworkdayjobs.com', 'wustl', 'External', {"jobFamily": ["5487990ab24101f0cdc401871b014a61", "5487990ab24101d410122e871b012062"]}),
    ('UW Madison', 'wisconsin.wd1.myworkdayjobs.com', 'wisconsin', 'UW_Madison', {}),
    ('UW Milwaukee', 'wisconsin.wd1.myworkdayjobs.com', 'wisconsin', 'UW_Milwaukee', {}),
    ('UW Comprehensives', 'wisconsin.wd1.myworkdayjobs.com', 'wisconsin', 'UW_Comprehensives', {"jobFamilyGroup": ["5adf054562b6101488460aac92210000"]}),
    ('Texas A&M University', 'tamus.wd1.myworkdayjobs.com', 'tamus', 'TAMU_External', {"jobFamilies": ["0e1cd8ed350201aa1bb93ba3f04b7816", "0e1cd8ed3502010480dd1fa3f04bca15"]}),
    ('Texas A&M System', 'tamus.wd1.myworkdayjobs.com', 'tamus', 'System-wide_External', {"jobFamilies": ["0e1cd8ed3502010480dd1fa3f04bca15", "0e1cd8ed350201aa1bb93ba3f04b7816"]}),
    ('Texas A&M System Offices', 'tamus.wd1.myworkdayjobs.com', 'tamus', 'TAMUS_External', {"jobFamilies": ["0e1cd8ed3502010480dd1fa3f04bca15"]}),
    ('Texas A&M AgriLife', 'tamus.wd1.myworkdayjobs.com', 'tamus', 'AgriLife_Research_External', {"jobFamilies": ["0e1cd8ed3502010480dd1fa3f04bca15"]}),
    ('Ohio State', 'osu.wd1.myworkdayjobs.com', 'osu', 'OSUCareers', {}),
    ('UChicago', 'uchicago.wd5.myworkdayjobs.com', 'uchicago', 'External', {"jobFamily": ["b27821c6151510033100e37e60e96ea1", "b27821c61515100331010b21bd1b6ec3"]}),
    ('UT Austin Staff', 'utaustin.wd1.myworkdayjobs.com', 'utaustin', 'UTstaff', {"jobFamily": ["dbb99aeb5bf3015adaa5fedb3c058e14"]}),
    ('UT Austin Student', 'utaustin.wd1.myworkdayjobs.com', 'utaustin', 'UTstudent', {}),
    ('USC', 'usc.wd5.myworkdayjobs.com', 'usc', 'ExternalUSCCareers', {"jobFamilyGroup": ["47619a3c24c0100ff960eb1fad17f797", "47619a3c24c0100ff960c541b135f787"]}),
    ('USC Keck Medicine', 'usc.wd5.myworkdayjobs.com', 'usc', 'ExternalKeckUSCCareers', {"jobFamilyGroup": ["47619a3c24c0100ff960eb1fad17f797", "47619a3c24c0100ff960c541b135f787"]}),
    ('University of Washington', 'uw.wd5.myworkdayjobs.com', 'uw', 'UWHires', {"jobFamily": ["5e955a616fd51001a3042cc61f7e0000", "77f9655292ed1001a3331d4626ef0000"]}),
    ('Cornell Cooperative Extension', 'cornell.wd1.myworkdayjobs.com', 'cornell', 'CCECareerPage', {}),
]


# ============ FACET CLEANUP ============
# Auto-discovery can accidentally pick physical security/police buckets.
# These are removed before scraping.
BAD_FACET_IDS = {
    # Walmart: Investigations and Security
    "e83ebdbd2a0a01050ff47e8948e912c6",
    # Barclays: Real Estate & Physical Security
    "112c054282011001e916114dbeec0000",
    # Texas A&M: Police / Security
    "0e1cd8ed350201aa1bb93ba3f04b7816",
    # UChicago: Police & Security
    "b27821c61515100331010b21bd1b6ec3",
    # University of Washington: Security/Police/Dispatch
    "77f9655292ed1001a3331d4626ef0000",
    # Mass General Brigham: generic security family, likely physical/security ops
    "1856eb1940d51000cc9eb3df6d780000",
}

# Known good Walmart filters. Keep the broad tech/data/engineering buckets + USA.
MANUAL_FACET_OVERRIDES = {
    "Walmart": {
        "jobFamilyGroup": [
            "e83ebdbd2a0a01e7e1477a8948e904c6",  # Technology
            "e83ebdbd2a0a01af0185848948e94dc6",  # Data Analytics and Management
            "e83ebdbd2a0a01ea72c2808948e924c6",  # Engineering and Design
        ],
        "locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"],  # United States
    },

    # Salesforce's UI uses this URL filter when you choose Country = United States.
    "Salesforce": {
        "CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended": [
            "bc33aa3152ec42d4995f4791a106ed09"
        ]
    }
}


def clean_facets(label, facets):
    facets = dict(facets or {})

    if label in MANUAL_FACET_OVERRIDES:
        facets = dict(MANUAL_FACET_OVERRIDES[label])

    cleaned = {}
    for param, values in facets.items():
        if not isinstance(values, list):
            continue
        kept = [v for v in values if v not in BAD_FACET_IDS]
        if kept:
            cleaned[param] = kept
    return cleaned


def normalize_companies(companies):
    """De-dupe exact Workday boards and apply facet cleanup."""
    deduped = {}
    for label, host, tenant, site, facets in companies:
        deduped[(host, tenant, site)] = (label, host, tenant, site, clean_facets(label, facets))
    return list(deduped.values())


COMPANIES = normalize_companies(COMPANIES)

# ============ FACET MODE ============
# For "all jobs", do NOT send jobFamilyGroup/jobFamily/category facets.
# Those are category filters and will hide valid jobs.
# Keep only country/location facets when available.
SALESFORCE_US_COUNTRY_FACET = "CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended"

LOCATION_FACET_KEYS = {
    "locationCountry",
    "Location_Country",
    "country",
    "Country",
    SALESFORCE_US_COUNTRY_FACET,
}


def is_location_facet_key(key):
    k = (key or "").lower()
    return (
        key in LOCATION_FACET_KEYS
        or "locationcountry" in k
        or "location_country" in k
        or "country_from_job_posting_location" in k
        or ("country" in k and "location" in k)
    )


def facets_for_scrape(facets):
    facets = facets or {}

    if USE_CATEGORY_FACETS:
        return facets

    return {
        k: v for k, v in facets.items()
        if is_location_facet_key(k)
    }


# ============ LOCATION FILTERS ============
AMBIGUOUS_LOCATION_PAT = re.compile(r"^\s*(\d+\s+locations?|multiple locations|various locations)\s*$", re.I)

US_PAT = re.compile("|".join([
    r"united states",
    r"united states of america",
    r"\bu\.?s\.?a?\b",
    r"\bremote\b",
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
]), re.I)

NON_US = re.compile("|".join([
    r"\bcanada\b", r"\bindia\b", r"\bchina\b", r"\btaiwan\b", r"\bkorea\b",
    r"\bjapan\b", r"\bnetherlands\b", r"\bgermany\b", r"\bunited kingdom\b",
    r"\bireland\b", r"\bsingapore\b", r"\bmexico\b", r"\bphilippines\b",
    r"\bmalaysia\b", r"\bbrazil\b", r"\bisrael\b", r"\baustralia\b",
    r"\bfrance\b", r"\bspain\b", r"\bpoland\b", r"\bswitzerland\b",
    r"\bbengaluru\b", r"\bhyderabad\b", r"\bpune\b", r"\bchennai\b",
]), re.I)


def location_allowed(loc):
    if not US_ONLY:
        return True

    l = (loc or "").strip()

    if NON_US.search(l):
        return False

    if US_PAT.search(l):
        return True

    if KEEP_AMBIGUOUS_LOCATIONS and AMBIGUOUS_LOCATION_PAT.search(l):
        return True

    return False


# ============ HELPERS ============
def parse_days(posted):
    p = (posted or "").lower().strip()

    if "today" in p:
        return 0
    if "yesterday" in p:
        return 1

    m = re.search(r"(\d+)\+?\s*day", p)
    if m:
        return int(m.group(1))

    return 999


def bucket(d):
    if d <= PRIORITY_DAYS:
        return "today-yesterday"
    if d <= MAX_DAYS:
        return "last-3-days"
    return f">{MAX_DAYS}"


def req_id(path):
    m = re.search(r"_(R-\d+(?:-\d+)?)", path or "")
    return m.group(1) if m else (path or "")


def apply_link(host, site, path):
    return f"https://{host}/en-US/{site}{path}"


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def log_error(row):
    with ERRORS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ============ FETCH ============
def fetch_recent(session, label, host, tenant, site, facets):
    """Fetch all titles posted within MAX_DAYS, with optional country-only facets."""
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    scrape_facets = facets_for_scrape(facets)
    out = []
    offset = 0
    seen = set()

    while offset < MAX_OFFSET:
        payload = {
            "appliedFacets": scrape_facets or {},
            "limit": PAGE,
            "offset": offset,
            "searchText": "",
        }

        try:
            r = session.post(endpoint, json=payload, headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()

        except requests.exceptions.Timeout:
            msg = f"{label}: TIMEOUT @ offset={offset}"
            print(f"  {msg}")
            log_error({"company": label, "endpoint": endpoint, "offset": offset, "error": msg})
            break

        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", "?")
            msg = f"{label}: HTTP {status} @ offset={offset}"
            print(f"  {msg}")
            log_error({"company": label, "endpoint": endpoint, "offset": offset, "status": status, "error": str(e)})
            break

        except requests.exceptions.ConnectionError:
            msg = f"{label}: CONNECTION_ERROR @ offset={offset}"
            print(f"  {msg}")
            log_error({"company": label, "endpoint": endpoint, "offset": offset, "error": msg})
            break

        except ValueError:
            msg = f"{label}: BAD_JSON @ offset={offset}"
            print(f"  {msg}")
            log_error({"company": label, "endpoint": endpoint, "offset": offset, "error": msg})
            break

        except Exception as e:
            msg = f"{label}: ERR {type(e).__name__} @ offset={offset}: {e}"
            print(f"  {msg}")
            log_error({"company": label, "endpoint": endpoint, "offset": offset, "error": msg})
            break

        posts = data.get("jobPostings", [])
        if not posts:
            break

        page_days = [parse_days(p.get("postedOn", "")) for p in posts]

        for p in posts:
            d = parse_days(p.get("postedOn", ""))
            if d > MAX_DAYS:
                continue

            loc = p.get("locationsText", "")
            if not location_allowed(loc):
                continue

            path = p.get("externalPath", "")
            if not path or path in seen:
                continue
            seen.add(path)

            out.append({
                "company": label,
                "title": p.get("title", ""),
                "posted": p.get("postedOn", ""),
                "days": d,
                "locations": loc,
                "link": apply_link(host, site, path),
                "path": path,
                "host": host,
                "tenant": tenant,
                "site": site,
                "facets_used": json.dumps(scrape_facets or {}, ensure_ascii=False),
            })

        if page_days and min(page_days) > MAX_DAYS:
            break

        if len(posts) < PAGE:
            break

        offset += PAGE
        time.sleep(SLEEP)

    return out


# ============ OUTPUT ============
def dump_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Company", "Bucket", "Posted", "Days", "New",
            "Role", "Location", "Link", "ReqId", "FacetsUsed"
        ])
        for j in rows:
            w.writerow([
                j["company"],
                j["_bucket"],
                j["posted"],
                j["_days"],
                j["_new"],
                j["title"],
                j["locations"],
                j["link"],
                j["req_id"],
                j.get("facets_used", ""),
            ])


def write_priority_md(path, rows, now):
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Priority: Today/Yesterday — {now.isoformat(timespec='seconds')}\n\n")
        f.write(f"{len(rows)} roles\n\n")
        f.write("| Company | Posted | New? | Role | Location | Link |\n")
        f.write("|---|---|---|---|---|---|\n")
        for j in rows:
            f.write(
                f"| {j['company']} | {j['posted']} | {'NEW' if j['_new'] else ''} | "
                f"{j['title']} | {j['locations']} | {j['link']} |\n"
            )


def write_daily_openings_file(db, now, new_keys_this_run):
    """
    One file per EST calendar day: data/openings_MMDDYYYY.md
    Fully regenerated each run from jobs_db.json, filtered to jobs whose
    first_seen date (in EST) == today. Jobs discovered in THIS run are
    tagged (NEW!). Jobs discovered earlier today are listed without the tag.
    When the EST date rolls over, a new file starts automatically because
    today_key changes — no separate "rollover" logic needed.
    """
    today_key = now.strftime("%m%d%Y")
    path = PROJECT_DIR / f"openings_{today_key}.md"

    todays_jobs = []
    for rec in db.values():
        try:
            first_seen_dt = datetime.fromisoformat(rec["first_seen"])
        except (KeyError, ValueError):
            continue
        if first_seen_dt.astimezone(EST).strftime("%m%d%Y") == today_key:
            todays_jobs.append(rec)

    todays_jobs.sort(key=lambda r: (r["company"], r["title"]))

    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Openings discovered {today_key} (America/New_York)\n\n")
        f.write(f"Last updated: {now.isoformat(timespec='seconds')}\n\n")
        f.write(f"{len(todays_jobs)} roles discovered today so far.\n\n")
        f.write("| Company | Title | Location | Posted | Link |\n")
        f.write("|---|---|---|---|---|\n")
        for r in todays_jobs:
            tag = " **(NEW!)**" if r["db_key"] in new_keys_this_run else ""
            f.write(f"| {r['company']} | {r['title']}{tag} | {r['locations']} | {r['posted']} | {r['link']} |\n")

    return path


# ============ RUN ============
def run_scraper():
    if ERRORS_JSONL.exists():
        ERRORS_JSONL.unlink()

    now = now_est()

    db = load_json(DB_FILE, {})
    prior_ids = set(db.keys())

    session = requests.Session()
    scraped = []
    raw_seen = set()

    print(f"\nStarting scrape across {len(COMPANIES)} Workday boards")
    print(f"Mode: ALL TITLES posted within {MAX_DAYS} days")
    print(f"Now (EST): {now.isoformat(timespec='seconds')}")
    print(f"US_ONLY: {US_ONLY}")
    print(f"KEEP_AMBIGUOUS_LOCATIONS: {KEEP_AMBIGUOUS_LOCATIONS}")
    print(f"USE_CATEGORY_FACETS: {USE_CATEGORY_FACETS}")
    print(f"Output folder: {PROJECT_DIR}")

    for i, (label, host, tenant, site, facets) in enumerate(COMPANIES, 1):
        print(f"\n[{i}/{len(COMPANIES)}] {label}")
        scrape_facets = facets_for_scrape(facets)
        print(f"  raw facets: {facets if facets else '{}'}")
        print(f"  scrape facets: {scrape_facets if scrape_facets else '{}'}")

        jobs = fetch_recent(session, label, host, tenant, site, facets)
        print(f"  kept {len(jobs)} recent jobs")

        for j in jobs:
            global_key = (j["host"], j["site"], j["path"])
            if global_key not in raw_seen:
                raw_seen.add(global_key)
                scraped.append(j)

        time.sleep(0.35)

    current = []
    new_jobs = []
    new_keys_this_run = set()

    for j in scraped:
        rid = req_id(j["path"]) or j["path"]
        db_key = f"{j['host']}::{j['site']}::{rid}"
        old_key = rid  # backward compatibility with older jobs_db.json formats
        is_new = db_key not in prior_ids and old_key not in prior_ids

        d = parse_days(j["posted"])
        rec = {
            **j,
            "req_id": rid,
            "db_key": db_key,
            "_days": d,
            "_bucket": bucket(d),
            "_new": is_new,
            "last_seen": now.isoformat(timespec="seconds"),
        }
        current.append(rec)

        if db_key in db:
            db[db_key].update({
                "last_seen": now.isoformat(timespec="seconds"),
                "posted": j["posted"],
                "locations": j["locations"],
                "link": j["link"],
                "title": j["title"],
                "company": j["company"],
                "db_key": db_key,
            })
        else:
            db[db_key] = {
                **j,
                "req_id": rid,
                "db_key": db_key,
                "first_seen": now.isoformat(timespec="seconds"),
                "last_seen": now.isoformat(timespec="seconds"),
            }
            if is_new:
                new_jobs.append(rec)
                new_keys_this_run.add(db_key)

    current.sort(key=lambda x: (x["_days"], x["company"], x["title"]))
    new_jobs.sort(key=lambda x: (x["_days"], x["company"], x["title"]))

    priority = [j for j in current if j["_days"] <= PRIORITY_DAYS]
    priority.sort(key=lambda x: (x["_days"], x["company"], x["title"]))

    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")

    RUN_FILE.write_text(json.dumps({
        "last_run": now.isoformat(timespec="seconds"),
        "boards_scraped": len(COMPANIES),
        "mode": "all_titles_last_3_days",
        "us_only": US_ONLY,
        "within_days": MAX_DAYS,
        "within_3d": len(current),
        "new_this_run": len(new_jobs),
        "priority_today_yesterday": len(priority),
    }, indent=2), encoding="utf-8")

    write_priority_md(PRIORITY_MD, priority, now)
    dump_csv(PRIORITY_CSV, priority)
    dump_csv(LAST3_CSV, current)
    dump_csv(ALL_RAW_CSV, current)
    daily_file = write_daily_openings_file(db, now, new_keys_this_run)

    print("\n=== SUMMARY ===")
    print(f"Boards scraped: {len(COMPANIES)}")
    print(f"Within {MAX_DAYS} days, all titles{' US-only' if US_ONLY else ''}: {len(current)}")
    print(f"New this run: {len(new_jobs)}")
    print(f"Priority today/yesterday: {len(priority)}")
    print(f"Wrote: {PRIORITY_MD}")
    print(f"Wrote: {PRIORITY_CSV}")
    print(f"Wrote: {LAST3_CSV}")
    print(f"Wrote: {ALL_RAW_CSV}")
    print(f"Wrote: {daily_file}")
    if ERRORS_JSONL.exists():
        print(f"Errors, if any: {ERRORS_JSONL}")

    if current and pd is not None:
        print("\nPreview:")
        preview_cols = ["company", "_bucket", "posted", "title", "locations", "link"]
        print(pd.DataFrame(current)[preview_cols].head(75).to_string(index=False))
    elif not current:
        print("No matches")

    return current, new_jobs, priority


if __name__ == "__main__":
    run_scraper()
