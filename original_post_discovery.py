import logging
import requests

from util import get_webmention_target, follow_redirects
from models import SyndicatedPost

from activitystreams import source as as_source
from appengine_config import HTTP_TIMEOUT, DEBUG
from mf2py.parser import Parser as Mf2Parser


def original_post_discovery(source, activity):
  """Augments the standard original_post_discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  Performs a reverse-lookup that scans the activity's author's h-feed
  for posts with rel=syndication links. As we find syndicated copies,
  save the relationship.  If we find the original pos for the activity
  in question, return the original's URL.

  See http://indiewebcamp.com/posse-post-discovery for more detail.

  Args:
    source: models.Source subclass
    activity: activity dict

  """

  as_source.Source.original_post_discovery(activity)

  note = activity.get('object', {})

  # Use source.domain_url for now; it seems more reliable than the
  # activity.actor.url (which depends on getting the right data back
  # from various APIs). Consider using the actor's url, with
  # domain_url as the fallback) in the future to support content from
  # non-Bridgy users.
  # author_url = activity.get('actor', {}).get('url')
  author_url = source.domain_url
  syndication_url = note.get('url')

  if not author_url:
    logging.debug("no author url, cannot find h-feed %s", author_url)
    return None

  if not syndication_url:
    logging.debug("no syndication url, cannot process h-entries %s",
                  syndication_url)
    return None

  if DEBUG:
    if author_url.startswith('http://snarfed.org'):
      author_url = author_url.replace('snarfed.org', 'localhost')
    elif author_url.startswith('http://kylewm.com'):
      author_url = author_url.replace('kylewm.com', 'localhost')

  # use the canonical syndication url on both sides, so that we have
  # the best chance of finding a match. Some silos allow several
  # different permalink formats to point to the same place (e.g.,
  # facebook user id instead of user name)
  syndication_url = follow_redirects(syndication_url).url

  logging.debug("posse post discovery with author %s and syndicated %s",
                author_url, syndication_url)

  relationship = SyndicatedPost.query_by_syndication(syndication_url)
  if not relationship:
    # a syndicated post we haven't seen before! fetch the author's
    # h-feed to see if we can find it.
    results = _process_author(author_url)
    relationship = results.get(syndication_url, None)

  if not relationship:
    # No relationship was found. Remember that we've seen this
    # syndicated post to avoid reprocessing it every time
    relationship = SyndicatedPost()
    relationship.syndication = syndication_url
    relationship.put()
    return None

  original = relationship.original

  if original:
    note.setdefault('tags', []).append({
        'objectType': 'article',
        'url': original,
      })

  return activity


def _process_author(author_url):
  """Fetch the author's domain URL, and look for syndicated posts.

  Return:
    a map from syndicated_url to models.SyndicatedPost

  """
  # for now use whether the url is a valid webmention target
  # as a proxy for whether it's worth searching it.
  # TODO skip sites we know don't have microformats2 markup
  _, _, is_valid_target = get_webmention_target(author_url)
  if not is_valid_target:
    return {}

  try:
    logging.debug("fetching author domain %s", author_url)
    author_resp = requests.get(author_url, timeout=HTTP_TIMEOUT)
  except BaseException:
    # TODO limit allowed failures, cache the author's h-feed url
    # or the # of times we've failed to fetch it
    logging.exception("Could not fetch author url %s", author_url)
    return {}

  author_parsed = Mf2Parser(url=author_url, doc=author_resp.text).to_dict()

  # look for canonical feed url (if it isn't this one) using
  # rel='feed', type='text/html'
  canonical = next(iter(author_parsed['rels'].get('feed', [])), None)
  if canonical and canonical != author_url:
    try:
      logging.debug("fetching author's canonical full feed %s", canonical)
      canonical_resp = requests.get(canonical, timeout=HTTP_TIMEOUT)
      author_parsed = Mf2Parser(
        url=canonical, doc=canonical_resp.text).to_dict()
    except BaseException:
      logging.exception(
        "Could not fetch h-feed url %s. Falling back on author url.",
        canonical)

  feeditems = author_parsed['items']
  hfeed = next((item for item in feeditems
                if 'h-feed' in item['type']), None)
  if hfeed:
    feeditems = hfeed.get('children')
  else:
    logging.info("No h-feed found, fallback to top-level h-entrys.")

  return _process_feed(feeditems)


def _process_feed(feeditems):
  """process each h-feed entry that has not been encountered before

  Args:
    feeditems: a list of mf2 dicts

  Return:
    a map from syndicated url to new models.SyndicatedPosts

  """
  permalinks = set()
  for child in feeditems:
    if 'h-entry' in child['type']:
      # TODO if this h-entry in the h-feed has u-syndication links, we
      # can just use it without fetching its permalink page
      for permalink in child['properties'].get('url', []):
        permalinks.add(permalink)

  results = {}
  for permalink in permalinks:  # TODO maybe limit to first ~30 entries?
    relationship = SyndicatedPost.query_by_original(permalink)
    # if the post hasn't already been processed
    if not relationship:
      logging.debug("processing permalink: %s", permalink)
      results.update(_process_entry(permalink))

  return results


def _process_entry(permalink):
  """Fetch and process an h-hentry, saving a new SyndicatedPost to the
  DB if successful.

  Args:
    permalink: url of the unprocessed post
    syndication_url: url of the syndicated content

  Return:
    a map from syndicated url to new models.SyndicatedPosts

  """
  try:
    logging.debug("fetching post permalink %s", permalink)
    resp = requests.get(permalink, timeout=HTTP_TIMEOUT)
    parsed = Mf2Parser(url=permalink, doc=resp.text).to_dict()
  except BaseException:
    # TODO limit the number of allowed failures
    logging.exception("Could not fetch permalink %s", permalink)
    return {}

  syndurls = set()
  relsynd = parsed.get('rels').get('syndication', [])
  logging.debug("rel-syndication links: %s", relsynd)
  syndurls.update(relsynd)

  hentry = next((item for item in parsed['items']
                 if 'h-entry' in item['type']), None)
  if hentry:
    usynd = hentry.get('properties', {}).get('syndication', [])
    logging.debug("u-syndication links: %s", usynd)
    syndurls.update(usynd)

  # save the results (or lack thereof) to the db, and put them in a
  # map for immediate use
  results = {}
  if syndurls:
    for syndurl in syndurls:
      # follow redirects to give us the canonical syndication url --
      # gives the best chance of finding a match.
      syndurl = follow_redirects(syndurl).url
      logging.debug("saving discovered relationship %s -> %s", syndurl, permalink)
      relationship = SyndicatedPost(original=permalink,
                                    syndication=syndurl)
      relationship.put()
      results[syndurl] = relationship
  else:
    # remember that this post doesn't have syndication links
    relationship = SyndicatedPost(original=permalink)
    relationship.put()

  return results
