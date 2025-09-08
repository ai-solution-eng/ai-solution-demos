#!/usr/local/bin/python3

import jwt
import time
import pytz
from datetime import datetime


token_path = "/etc/secrets/ezua/.auth_token"
with open(token_path, 'r') as f:
    token = f.read()
try:
    decoded = jwt.decode(token, options={"verify_signature": False})
    exp = decoded["exp"]
    system_tz = time.tzname[0]
    tz = pytz.timezone(system_tz)
    ezaf_token_exp_time = datetime.fromtimestamp(int(exp), tz).isoformat()
    
    creds_templ = f'''
  "Version": 1,
  "AccessKeyId": "{token}",
  "SecretAccessKey": "s3",
  "SessionToken": "",
  "Expiration": "{ezaf_token_exp_time}"
'''
    creds = "{" + creds_templ + "}"
    
    print(creds)
except subprocess.SubprocessError as e:
    print("Decode of JWT failed.")
    logging.error(e)