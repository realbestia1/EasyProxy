# Adapted for use in EasyProxy from:
# https://github.com/einars/js-beautify/blob/master/python/jsbeautifier/unpackers/packer.py
# Unpacker for Dean Edward's p.a.c.k.e.r, a part of javascript beautifier
# by Einar Lielmanis <einar@beautifier.io>
#
#     written by Stefano Sanfilippo <a.little.coder@gmail.com>
#
# usage:
#
# if detect(some_string):
#     unpacked = unpack(some_string)
#
"""Unpacker for Dean Edward's p.a.c.k.e.r"""

import asyncio
import logging
import re
from aiohttp_socks import ProxyError as AioProxyError
from bs4 import BeautifulSoup, SoupStrainer
from python_socks import ProxyError as PyProxyError
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


def detect(source):
    if "eval(function(p,a,c,k,e,d)" in source:
        mystr = "smth"
        return mystr is not None


def unpack(source):
    """Unpacks P.A.C.K.E.R. packed js code."""
    payload, symtab, radix, count = _filterargs(source)

    if count != len(symtab):
        raise UnpackingError("Malformed p.a.c.k.e.r. symtab.")

    try:
        unbase = Unbaser(radix)
    except TypeError:
        raise UnpackingError("Unknown p.a.c.k.e.r. encoding.")

    def lookup(match):
        """Look up symbols in the synthetic symtab."""
        word = match.group(0)
        return symtab[unbase(word)] or word

    payload = payload.replace("\\\\", "\\").replace("\\'", "'")
    source = re.sub(r"\b\w+\b", lookup, payload)
    return _replacestrings(source)


def _filterargs(source):
    """Juice from a source file the four args needed by decoder."""
    juicers = [
        (r"}\('(.*)', *(\d+|\[\]), *(\d+), *'(.*)'\.split\('\|'\), *(\d+), *(.*)\)\)"),
        (r"}\('(.*)', *(\d+|\[\]), *(\d+), *'(.*)'\.split\('\|'\)"),
    ]
    for juicer in juicers:
        args = re.search(juicer, source, re.DOTALL)
        if args:
            a = args.groups()
            if a[1] == "[]":
                a = list(a)
                a[1] = 62
                a = tuple(a)
            try:
                return a[0], a[3].split("|"), int(a[1]), int(a[2])
            except ValueError:
                raise UnpackingError("Corrupted p.a.c.k.e.r. data.")

    # could not find a satisfying regex
    raise UnpackingError(
        "Could not make sense of p.a.c.k.e.r data (unexpected code structure)"
    )


def _replacestrings(source):
    """Strip string lookup table (list) and replace values in source."""
    match = re.search(r'var *(_\w+)\=\["(.*?)"\];', source, re.DOTALL)

    if match:
        varname, strings = match.groups()
        startpoint = len(match.group(0))
        lookup = strings.split('","')
        variable = "%s[%%d]" % varname
        for index, value in enumerate(lookup):
            source = source.replace(variable % index, '"%s"' % value)
        return source[startpoint:]
    return source


class Unbaser(object):
    """Functor for a given base. Will efficiently convert
    strings to natural numbers."""

    ALPHABET = {
        62: "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        95: (
            " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
        ),
    }

    def __init__(self, base):
        self.base = base

        # fill elements 37...61, if necessary
        if 36 < base < 62:
            if not hasattr(self.ALPHABET, self.ALPHABET[62][:base]):
                self.ALPHABET[base] = self.ALPHABET[62][:base]
        # attrs = self.ALPHABET
        # print ', '.join("%s: %s" % item for item in attrs.items())
        # If base can be handled by int() builtin, let it do it for us
        if 2 <= base <= 36:
            self.unbase = lambda string: int(string, base)
        else:
            # Build conversion dictionary cache
            try:
                self.dictionary = dict(
                    (cipher, index) for index, cipher in enumerate(self.ALPHABET[base])
                )
            except KeyError:
                raise TypeError("Unsupported base encoding.")

            self.unbase = self._dictunbaser

    def __call__(self, string):
        return self.unbase(string)

    def _dictunbaser(self, string):
        """Decodes a  value to an integer."""
        ret = 0
        for index, cipher in enumerate(string[::-1]):
            ret += (self.base ** index) * self.dictionary[cipher]
        return ret


class UnpackingError(Exception):
    """Badly packed source or general error. Argument is a
    meaningful description."""
    pass


async def eval_solver(session, url: str, headers: dict, patterns: list[str]) -> str:
    try:
        async with session.get(url, headers=headers) as response:
            text = await response.text()

        # Check for common error messages indicating video not found or unavailable
        error_indicators = [
            "can't find the video",
            "video you are looking for",
            "file was deleted",
            "file not found",
            "this file does not exist",
            "video not found"
        ]

        text_lower = text.lower()
        for indicator in error_indicators:
            if indicator in text_lower:
                logger.warning("Video not available at %s: detected '%s'", url, indicator)
                raise UnpackingError(f"Video not found or unavailable at {url}")

        # Try to find and unpack JavaScript
        soup = BeautifulSoup(text, "lxml", parse_only=SoupStrainer("script"))
        script_all = soup.find_all("script")

        packed_scripts = []
        for i in script_all:
            if i.text and detect(i.text):
                packed_scripts.append(i.text)

        if not packed_scripts:
            logger.warning("No packed JavaScript found at %s. Page may have changed structure.", url)
            raise UnpackingError(
                f"No packed JavaScript found at {url}. The video may not exist or the page structure has changed.")

        # Try to extract URL from packed scripts
        for script in packed_scripts:
            try:
                unpacked_code = unpack(script)
                logger.debug("Unpacked code snippet: %s", unpacked_code[:200])

                for pattern in patterns:
                    match = re.search(pattern, unpacked_code)
                    if match:
                        extracted_url = match.group(1)
                        if not urlparse(extracted_url).scheme:
                            extracted_url = urljoin(url, extracted_url)

                        logger.info("Successfully extracted URL from %s", url)
                        return extracted_url
            except Exception as unpack_error:
                logger.debug("Failed to unpack script: %s", str(unpack_error))
                continue

        # If we got here, we found packed JS but couldn't extract the URL
        logger.warning("Found packed JavaScript but no patterns matched at %s. Patterns tried: %s", url, patterns)
        raise UnpackingError(
            f"Found packed JavaScript but could not extract video URL. The extraction patterns may need updating.")

    except (UnpackingError, AioProxyError, PyProxyError, asyncio.TimeoutError):
        raise
    except Exception as e:
        logger.exception("Unexpected error in eval_solver for %s", url)
        raise UnpackingError(f"Error extracting from {url}: {str(e)}") from e
