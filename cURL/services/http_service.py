from io import BytesIO

import pycurl


class HttpsService:
  def __init__(self, base_url: str = ""):
    self.base_url = base_url

  def measure(self, url: str) -> int:
    target_url = f"{self.base_url}{url}"
    buffer = BytesIO()
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, target_url)
    curl.setopt(pycurl.WRITEDATA, buffer)
    curl.setopt(pycurl.FOLLOWLOCATION, True)
    curl.perform()
    time = curl.getinfo(pycurl.TOTAL_TIME)
    curl.close()
    return int(time * 1000)
