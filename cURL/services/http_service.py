from io import BytesIO

import pycurl


class HttpsService:
  def __init__(self, base_url: str = ""):
    self.base_url = base_url

  def _http_version(self, quic: bool) -> int:
    if quic:
      if hasattr(pycurl, "CURL_HTTP_VERSION_3ONLY"):
        return pycurl.CURL_HTTP_VERSION_3ONLY
      if hasattr(pycurl, "CURL_HTTP_VERSION_3"):
        return pycurl.CURL_HTTP_VERSION_3
      if hasattr(pycurl, "HTTP_VERSION_3ONLY"):
        return pycurl.HTTP_VERSION_3ONLY
      if hasattr(pycurl, "HTTP_VERSION_3"):
        return pycurl.HTTP_VERSION_3
      raise RuntimeError("HTTP/3 (QUIC) is not supported by this libcurl build")

    if hasattr(pycurl, "CURL_HTTP_VERSION_2TLS"):
      return pycurl.CURL_HTTP_VERSION_2TLS
    if hasattr(pycurl, "CURL_HTTP_VERSION_2_0"):
      return pycurl.CURL_HTTP_VERSION_2_0
    if hasattr(pycurl, "CURL_HTTP_VERSION_2"):
      return pycurl.CURL_HTTP_VERSION_2
    if hasattr(pycurl, "HTTP_VERSION_2TLS"):
      return pycurl.HTTP_VERSION_2TLS
    if hasattr(pycurl, "HTTP_VERSION_2_0"):
      return pycurl.HTTP_VERSION_2_0
    if hasattr(pycurl, "HTTP_VERSION_2"):
      return pycurl.HTTP_VERSION_2
    raise RuntimeError("HTTP/2 is not supported by this libcurl build")

  def measure(self, url: str, quic: bool = False) -> int:
    target_url = f"{self.base_url}{url}"
    buffer = BytesIO()
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, target_url)
    curl.setopt(pycurl.WRITEDATA, buffer)
    curl.setopt(pycurl.FOLLOWLOCATION, True)
    curl.setopt(pycurl.HTTP_VERSION, self._http_version(quic))
    curl.perform()
    time = curl.getinfo(pycurl.TOTAL_TIME)
    curl.close()
    return int(time * 1000)
