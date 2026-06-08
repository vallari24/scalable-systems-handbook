# Designing Instagram

**Question: how do we build a social network for photo sharing?**

This post will build Instagram incrementally, focusing on three core workflows:

```text
uploading photos
serving photos through a CDN
discovering photos through hashtags
```

We will start with the smallest useful design:

```text
user -> API server -> photo storage -> feed/read path
```

Then we will add the components only when the baseline has a clear bottleneck.

## Why Instagram Is A Good Case Study

Facebook made the classic web stack famous:

```text
PHP application code
MySQL database
Memcached cache
```

By 2006, that was the mental model many web applications copied.

Instagram made a different startup stack famous.

In a little over one year, Instagram reached roughly 14 million users with a very small engineering team. The interesting part was not that every component was exotic. The interesting part was that the architecture stayed simple while handling real social-network scale.

The early shape looked like this:

```text
client
  -> Nginx / load balancer
  -> Django application
  -> PostgreSQL for users, photo metadata, tags
  -> Cassandra for high-volume wide-row data paths
  -> Redis for feeds, activity, sessions
  -> Memcached for cache
  -> S3 + CDN for photo bytes
  -> task queue + workers for slow background work
```

![Instagram year one architecture](../assets/social-network-instagram/instagram-year-one-architecture.svg)

Some implementation details evolved over time. Early Instagram used Gearman for task queues; later versions of this style are often shown with Celery and RabbitMQ. The mental model is the same:

```text
one monolithic application
one async processing path
one primary relational metadata store
one cache layer
one queue to share slow work between workers
```

That is the architecture we will build up from.

## What Is A CDN?

**Question: why should every photo request enter our backend?**

Suppose our origin service lives in the US:

```text
https://media.picshare.example/photos/42.jpg
```

The <span style="color:#ff8a8a"><strong>origin</strong></span> could be an API server, an S3 bucket, an object storage service, or any backend that returns a response.

When a user opens that URL, the browser resolves the domain to an IP address, connects to that IP address, and sends the HTTP request.

If the user is in India and the origin is in the US, the request may cross the ocean just to fetch an image that many other users have already fetched.

A <span style="color:#93c5fd"><strong>CDN</strong></span> fixes this by sitting transparently between the user and the <span style="color:#ff8a8a"><strong>origin</strong></span>.

```text
user -> CDN edge -> origin
```

The <span style="color:#93c5fd"><strong>CDN</strong></span> gives us a different public URL:

```text
https://cdn.picshare.example/photos/42.jpg
```

Inside the <span style="color:#ffff99"><strong>CDN configuration</strong></span>, we map that CDN domain back to the <span style="color:#ff8a8a"><strong>origin</strong></span>:

```text
cdn.picshare.example -> media.picshare.example
```

Now the frontend loads the CDN URL instead of the origin URL.

**Question: where does the CDN URL come from?**

The <span style="color:#93c5fd"><strong>CDN URL</strong></span> usually comes from the HTML, JSON, or API response returned by your <span style="color:#ff8a8a"><strong>origin application</strong></span>.

This does not mean every request must go through the CDN.

The HTML page can still come from the origin application:

```text
GET https://www.picshare.example/profile/ava
```

That request goes to the <span style="color:#ff8a8a"><strong>origin web server</strong></span> and returns HTML.

Inside that HTML, the image URL can point to the CDN:

```html
<img src="https://cdn.picshare.example/photos/42.jpg" />
```

So the browser makes two different requests:

```text
HTML document -> www.picshare.example      -> origin app
photo bytes   -> cdn.picshare.example      -> CDN edge
```

That is the common pattern. The application returns the page, but static or <span style="color:#8aff8a"><strong>cacheable assets</strong></span> inside the page come from the <span style="color:#93c5fd"><strong>CDN</strong></span>.

In a CDN control panel, the configuration usually looks like this:

| CDN field | Example |
| --- | --- |
| **Name** | `picshare-photos` |
| **Origin** | `https://media.picshare.example` |
| **CDN domain** | `https://cdn.picshare.example` |
| **Tier / plan** | `standard` |

If the <span style="color:#93c5fd"><strong>CDN</strong></span> already has `/photos/42.jpg`, it returns the response immediately from the nearest edge location.

If the <span style="color:#93c5fd"><strong>CDN</strong></span> does not have it, it forwards the request to the <span style="color:#ff8a8a"><strong>origin</strong></span>, gets the response, stores it in the <span style="color:#8aff8a"><strong>edge cache</strong></span>, and returns it to the user.

![CDN cache between user and origin](../assets/social-network-instagram/cdn-cache-between-user-and-origin.svg)

The important idea is:

```text
first request  -> CDN miss -> origin -> cache response -> return response
second request -> CDN hit  -> return response without touching origin
```

A CDN can cache more than images.

It can cache:

```text
images
videos
HTML
CSS
JavaScript
JSON
API responses
raw bytes
```

For Instagram, the most obvious win is photo delivery. A photo may be uploaded once, but viewed thousands or millions of times. We do not want every view to enter Django, PostgreSQL, Redis, or object storage directly.

### Cache Invalidation

**Question: what if the origin response changes?**

The <span style="color:#93c5fd"><strong>CDN</strong></span> has an old cached copy until we tell it otherwise.

There are two common ways to invalidate cached data.

The first is <span style="color:#ffff99"><strong>TTL-based invalidation</strong></span>:

```text
cache /photos/42.jpg for 1 hour
after 1 hour, ask the origin again
```

This is simple and cheap, but stale data can stay visible until the TTL expires.

The second is <span style="color:#ff8bd2"><strong>explicit invalidation</strong></span>:

```text
POST /cdn/invalidate
path = /photos/42.jpg
```

The CDN removes that cached path, so the next request fetches a fresh copy from the origin.

Some CDNs also support wildcard invalidation:

```text
path = /profiles/ava/*
path = /*
```

Use wildcard invalidation carefully. Invalidating too much can create a burst of cache misses and send a sudden wave of traffic back to the origin.

## Image Upload Service

**Question: how should a user upload a photo?**

Let us start with one concrete requirement:

```text
5 million photo uploads per day
```

That is not just an API problem. Uploading photos touches several parts of the system:

```text
storage
request flow
metadata
privacy
background processing
CDN serving
future extensibility
cost optimization
```

### The Basics

**Question: where should the image bytes live?**

Not in the application database.

Image bytes should live in an object store such as <span style="color:#ffff99"><strong>S3</strong></span>, GCS, Azure Blob Storage, or an S3-compatible storage system.

S3 is object storage, not block storage. We store one object at a key:

```text
bucket: picshare-photos
key:    originals/user-123/p_8vK2.jpg
value:  raw image bytes
```

The relational database stores metadata:

```text
photo_id
owner_user_id
caption
visibility
created_at
processing_status
```

So the first simple answer is:

```text
metadata -> database
image bytes -> object storage
```

**Question: how do the bytes reach object storage?**

The most obvious design is:

```text
user -> image upload service -> S3
```

The user sends an HTTP POST request. The image binary is inside the HTTP body, often as `multipart/form-data` or raw bytes.

The upload service receives the request, reads the image body, and then writes that image to S3.

A naive implementation may temporarily store the image on local disk:

```text
POST /photos
body = a.jpg

upload service:
  read request body
  write /tmp/uploads/a.jpg
  upload /tmp/uploads/a.jpg to S3
  create metadata row
  delete temp file
```

Some frameworks buffer request bodies in memory before the application code sees them. Others stream to disk. Either way, the upload service is now responsible for moving large bytes.

**Should the upload bytes go through our API server?**

This design works, but it mixes two very different jobs:

```text
control-plane work: authentication, permissions, metadata, database writes
data-plane work: large image bytes moving over the network
```

The control plane is small and logical.

The data plane is heavy and physical.

For one 2 MB photo, the upload service may pay for:

```text
2 MB ingress from the user
2 MB temporary memory or disk pressure
2 MB egress from upload service to S3
```

That is already twice the network movement through our backend.

At 5 million uploads per day, a 2 MB average photo means:

```text
5,000,000 * 2 MB = about 10 TB/day of uploaded image bytes
```

If the upload service relays every image to S3, the backend network path can see roughly:

```text
10 TB/day client -> upload service
10 TB/day upload service -> S3
```

That is before thumbnails, retries, videos, larger images, and peak traffic.

Memory also becomes a practical limit.

If a machine has 4 GB of RAM and each active upload body is 2 MB, the theoretical upper bound is:

```text
4 GB / 2 MB = about 2000 concurrent request bodies
```

The real number is much lower because the process, runtime, TLS buffers, request metadata, framework overhead, and other requests also need memory.

Disk can become a bottleneck too.

If the service writes every upload to `/tmp` before sending it to S3, then the machine needs enough local disk for active uploads, retries, and cleanup. A crash can leave partial files behind. A traffic spike can fill the disk and take the service down.

So this approach has a clear tradeoff.

| Upload through API server | Notes |
| --- | --- |
| **Pro** | Client is unaware of S3, buckets, keys, and internal storage layout. |
| **Pro** | API server can authenticate, validate, compress, scan, and transform before storage. |
| **Con** | Backend pays ingress and egress bandwidth for the same image. |
| **Con** | Upload service carries memory, disk, CPU, and network pressure. |
| **Con** | More upload traffic means more machines just to relay bytes. |
| **Con** | Temporary disk files need cleanup, retry handling, and crash recovery. |

Compression can help. If the upload service compresses images before storing them, we reduce storage and future delivery cost.

But compression is CPU work. If every image still flows through the upload service, compression can reduce one bottleneck while increasing another.

Chunked uploads can also help. They make retries cheaper because a failed upload can retry one chunk instead of the whole image.

But chunking does not remove the upload service from the byte path. It only changes how the bytes are broken up.

### Why Not Let The User Upload Directly?

**Question: why not expose the S3 bucket and let the user upload there?**

Because we cannot make the bucket public for writes.

If random clients can write directly to object storage, they can cause serious problems:

```text
upload arbitrary files
overwrite someone else's object key
upload huge files and create cost spikes
write private content into public paths
skip validation, rate limits, and abuse checks
reuse the same upload path many times
```

A per-user bucket is not a good fix.

Creating buckets, policies, quotas, lifecycle rules, and monitoring for millions of users would make storage operations more complicated than the upload problem itself.

We need a way to let a user upload exactly one allowed object, for a short time, without making S3 public.

That hints at the direction of the next section, but we should not jump there yet.

### Why Not Proxy The Connection?

**Question: can the upload service just bridge the user's connection to S3?**

That sounds like a load balancer idea:

```text
user -> upload service -> S3
```

The upload service receives the request, opens another connection to S3, and forwards the bytes.

But this is still proxying.

The bytes still enter the upload service and leave the upload service. We still pay the same backend bandwidth cost. We still keep the upload service in the hot data path.

Also, HTTP and TLS terminate at a specific endpoint. The upload service cannot magically hand an already-started upload over to S3 and disappear from the middle.

So the question becomes sharper:

```text
Can the API server authorize the upload,
without carrying the upload bytes?
```

That is the problem the next section will solve.

## Uploading Photos To S3 Using A Pre-Signed URL

**Question: can the API server authorize the upload without carrying the upload bytes?**

Yes. The API server can create a short-lived upload permission and give it to the browser.

In S3, this is usually done with a <span style="color:#ffff99"><strong>pre-signed URL</strong></span> or a <span style="color:#ffff99"><strong>pre-signed POST policy</strong></span>.

The idea is simple:

```text
API server signs one specific upload.
Browser uploads directly to S3.
S3 accepts the upload only if the signature is valid.
```

The bucket is still private. We are not making S3 public.

We are giving the client a temporary, scoped permission:

```text
allowed method: PUT or POST
allowed path:   s3://picshare-photos/user-123/img-abc.jpg
expires in:     5 minutes
max size:       5 MB
content type:   image/jpeg
```

Anyone holding that signed upload permission can use it until it expires, so we keep the expiry short and the path specific.

### The Flow

First, the browser asks the image service to prepare an upload.

It does not send the image bytes yet.

It sends small metadata:

```text
POST /photos/prepare-upload

{
  "filename": "beach.jpg",
  "size": 2384211,
  "content_type": "image/jpeg"
}
```

The image service checks the request:

```text
is the user logged in?
is the file type allowed?
is the file size allowed?
what S3 key should this photo use?
what DB row should represent this upload?
```

Then the image service creates a random photo ID and a private object key.

The object key can be computed from the user ID and photo ID:

```text
photo_id = "p_8vK2"
s3_key   = "originals/user-123/p_8vK2.jpg"

path pattern:
s3://picshare-photos/originals/<user_id>/<photo_id>.jpg
```

It creates a pending metadata row:

```text
photo_id: p_8vK2
owner: user-123
status: waiting_for_upload
```

Then it asks S3 to sign an upload permission for that exact key.

The response sent back to the browser looks like this:

```json
{
  "photo_id": "p_8vK2",
  "upload_url": "https://picshare-photos.s3.amazonaws.com/originals/user-123/p_8vK2.jpg?...signature...",
  "method": "PUT",
  "headers": {
    "Content-Type": "image/jpeg"
  },
  "cdn_url": "https://cdn.picshare.example/photos/p_8vK2.jpg"
}
```

Now the browser sends the image bytes directly to S3:

```text
PUT upload_url
Content-Type: image/jpeg
body: raw image bytes
```

S3 validates the signature. If the signature is valid, S3 stores the object at the allowed key and returns success:

```text
204 No Content
```

The byte-heavy request did not go through the image service.

### Text Diagram

```text
1. prepare upload

browser
  -> image service
     metadata only: filename, size, content_type

image service
  -> database
     create pending photo row

image service
  -> S3
     create signed upload permission for one object key

image service
  -> browser
     upload_url + required headers/fields + photo_id

2. upload bytes

browser
  -> S3
     raw image bytes on signed URL

S3
  -> browser
     204 No Content

3. serve later

browser
  -> CDN
     read photo URL

CDN
  -> S3/origin
     fetch on cache miss, then cache
```

Notice the separation:

```text
metadata request -> image service
image bytes      -> S3
read traffic     -> CDN
```

We do not upload the photo to the CDN.

The CDN is for serving cached reads. The upload goes to object storage.

### Store The Image ID, Not The Full URL

**Question: should the posts table store the full image URL?**

No.

A post is different from an image upload.

The image upload service owns the image. It creates the image ID and knows how to compute the storage path:

```text
image_id = p_8vK2

S3 path:
s3://picshare-photos/originals/<user_id>/<image_id>.jpg

CDN URL:
https://cdn.picshare.example/photos/<image_id>.jpg
```

The posts service should keep a reference to that image:

```text
posts

id        -> post ID
user_id   -> author of the post
image_id  -> image ID from image service
caption
created_at
```

It should not store this:

```text
https://cdn.picshare.example/photos/p_8vK2.jpg
```

The URL is derived at read time.

That seems like a small detail, but it gives us important flexibility.

If the CDN domain changes, we do not update billions of post rows.

If the storage backend changes from AWS S3 to an internal object store, we do not rewrite every photo URL.

If Instagram is acquired and the new company wants to serve images from a different CDN, the URL builder changes in one place.

If private photos need signed read URLs, the read path can generate them dynamically.

The database stores the stable identity:

```text
image_id
```

The application derives the unstable delivery location:

```text
cdn domain + path pattern + image_id
```

The posts service can still validate the image before creating the post:

```text
1. user creates a post with image_id = p_8vK2
2. posts service asks image service:
   does p_8vK2 belong to this user and is it uploaded?
3. if yes, posts service inserts the post row
```

This keeps ownership clean:

```text
image service -> owns image IDs and storage paths
posts service -> owns posts and references image IDs
CDN           -> serves derived read URLs
```

### Pre-Signed URL vs Pre-Signed POST

There are two common shapes.

The first shape is a pre-signed PUT URL:

```text
PUT https://bucket.s3.amazonaws.com/key?X-Amz-Signature=...
body = image bytes
```

The signature is in the URL. The browser uploads the file as the request body.

The second shape is a pre-signed POST policy:

```text
POST https://bucket.s3.amazonaws.com/
multipart form:
  key
  policy
  x-amz-credential
  x-amz-signature
  content-type
  file
```

The `policy` is not a certificate. It is a signed document that tells S3 what this upload is allowed to do.

For example, the policy can say:

```text
only upload to this key prefix
only accept image/jpeg
only accept files under 5 MB
expire this permission soon
```

S3 verifies the policy and signature before accepting the upload.

Both designs keep the same mental model:

```text
API server authorizes.
S3 receives bytes.
CDN serves reads.
```

### Real Product Pattern

You can see this pattern in many web products.

For example, when a product lets you attach an image to a comment, the browser often makes a small request first:

```text
POST /upload/policies/assets
```

That request sends metadata like:

```text
filename
size
content_type
authenticity_token
repository_id or owner_id
```

It does not need to send the full image body yet.

The response contains an upload target and final asset metadata:

```text
upload_url: storage upload endpoint
asset.id: generated asset ID
asset.href: final CDN/read URL
```

Then the browser makes another request directly to the storage upload endpoint. That second request carries the image bytes. A successful upload may return `204 No Content`.

This is the same shape we want:

```text
small policy request to app
large byte upload to storage
final reads through CDN
```

The image service stays in control without becoming the network pipe for every photo.

## Privacy For Photo Reads

**Question: are private photos private just because the object key is hard to guess?**

No.

An unguessable URL is not enough privacy.

If a private image URL is long-lived and someone shares it, any browser holding that URL may be able to fetch the photo.

For public photos, this is fine:

```text
https://cdn.picshare.example/photos/p_8vK2.jpg
```

For private photos, the read URL should be short-lived and signed.

The app first checks whether the viewer can see the photo:

```text
is viewer the owner?
does viewer follow the private account?
has the owner blocked the viewer?
is the post still visible?
```

Only after that check does the app return a signed read URL:

```text
https://cdn.picshare.example/photos/p_8vK2.jpg
  ?expires=1717700000
  &signature=abc123
  &cache_key=...
```

The exact query parameters differ by CDN. Some systems use names like `expires`, `signature`, `key`, `policy`, or cache-related fields.

The mental model is:

```text
photo URL + expiry + signature
```

When the browser renders the image tag:

```html
<img src="https://cdn.picshare.example/photos/p_8vK2.jpg?expires=...&signature=..." />
```

the request goes to the CDN.

The CDN validates the attached signature and expiry:

```text
if signature is valid and not expired:
  return the image
else:
  return 403 / bad URL
```

This keeps most photo reads on the CDN while still making private reads expire quickly.

Important detail:

```text
Upload signing lets the browser write one object to S3.
Read signing lets the browser read one object through CDN.
```

They are similar ideas, but they protect opposite directions.

The database still stores only the stable ID:

```text
image_id = p_8vK2
```

The read path derives the URL and attaches the short-lived signature at response time:

```text
image_id -> storage path -> CDN URL -> signed CDN URL
```

This also keeps us decoupled.

If we move from one CDN to another, we change the URL-signing code. We do not rewrite every post row.

If we move the origin from S3 to internal object storage, we change the storage-path builder. We do not rewrite every post row.

Private URLs are short-lived because the permission is short-lived.

If someone copies the signed URL, it may work for a little while. After expiry, the CDN rejects it and the viewer must ask the app for a fresh URL, where the app can run the privacy check again.

## Image Optimizations

**Question: should every user receive the original uploaded image?**

No.

Users are on different networks, devices, screens, and geographies.

Sending a 5 MB original photo to every follower is wasteful:

```text
small phone screen -> does not need original resolution
slow network       -> needs smaller bytes
profile thumbnail  -> needs a tiny crop
feed image         -> needs a medium version
zoom/fullscreen    -> may need a larger version
```

So the read path should be able to serve different versions of the same photo.

The simplest approach is to use CDN image transformation:

```text
https://cdn.picshare.example/photos/p_8vK2.jpg?w=360
```

The CDN fetches the original image from the origin, transforms it to 360 px width, caches that transformed version, and returns it to the user.

The next user requesting the same size gets the cached transformed image:

```text
first request  -> CDN miss -> transform -> cache 360px version
second request -> CDN hit  -> return cached 360px version
```

This gives us optimization without building our own image optimizer service on day one.

Common transformations include:

```text
resize
crop
format conversion: jpeg, webp, avif
quality reduction
thumbnail generation
```

The app can choose URLs based on context:

```text
feed card:      /photos/p_8vK2.jpg?w=720
profile grid:   /photos/p_8vK2.jpg?w=240
thumbnail:      /photos/p_8vK2.jpg?w=120
high-res view:  /photos/p_8vK2.jpg?w=1440
```

The important idea is:

```text
store one original
serve many optimized derivatives
cache each derivative at the CDN
```

Later, if CDN transformations become too expensive or too slow, we can add our own background image processing pipeline. But the first version should lean on the CDN feature if it is available.

## Overall Photo Flow

**Question: how does the whole photo flow fit together?**

At this point we have enough pieces to draw the full path.

Read the diagram by color:

```text
yellow -> upload preparation and image identity
blue   -> direct image-byte upload to S3
pink   -> post creation and post reads
green  -> CDN image reads
orange -> async events for downstream systems
```

![Instagram photo overall flow](../assets/social-network-instagram/instagram-photo-overall-flow.svg)

### 1. Upload The Image

User A first talks to the <span style="color:#ffff99"><strong>Image Service</strong></span>.

This request is not the image upload yet. It is just a prepare step:

```text
filename
content_type
size
owner_user_id
```

The <span style="color:#ffff99"><strong>Image Service</strong></span> creates an `image_id`, creates a pending image row, computes the S3 key, and returns a signed S3 upload URL.

Then the browser sends the image bytes <span style="color:#93c5fd"><strong>directly to S3</strong></span>.

```text
metadata -> Image Service
bytes    -> S3
```

### 2. Create The Post

Uploading the image is not the same as posting it.

After the image exists, User A creates a post through the <span style="color:#ff8bd2"><strong>Posts Service</strong></span>.

The post request contains post-level information:

```text
caption
visibility
author_user_id
image_id
```

The <span style="color:#ff8bd2"><strong>Posts Service</strong></span> owns the post record. It stores things like caption, author, visibility, timestamps, and the `image_id`.

It does not own image bytes.

Before inserting the post, it can ask the Image Service:

```text
does this image_id exist?
does it belong to this author?
has upload completed?
```

Then it stores the post row:

```text
posts

id
author_user_id
caption
visibility
image_id
created_at
```

### 3. Read The Post

Now User B visits User A's profile:

```text
GET /users/A/posts
```

The <span style="color:#ff8bd2"><strong>Posts Service</strong></span> reads the post rows and returns post data.

The backend dynamically derives the image URL from `image_id`:

```text
image_id -> CDN URL
```

The response might contain:

```json
{
  "id": "post_123",
  "author_user_id": "A",
  "caption": "sunset",
  "image_url": "https://cdn.picshare.example/photos/p_8vK2.jpg?w=720"
}
```

The browser renders that URL in an `<img>` tag.

Then the browser makes a separate request to the <span style="color:#8aff8a"><strong>CDN</strong></span>.

The CDN either serves the image from cache or fetches it from S3, transforms it if needed, caches it, and returns it.

### 4. Why Kafka?

**Question: why do we use Kafka here?**

Creating a post should be fast.

The synchronous path should do only the work required to tell the user:

```text
your post was created
```

But many other systems care about the new post:

```text
search indexing
notifications
feed fanout
analytics
abuse detection
recommendation signals
```

If the Posts Service calls all of those systems directly, post creation becomes slow and fragile.

Kafka gives us an async boundary:

```text
Posts Service -> post_created event -> Kafka -> downstream services
```

Downstream services can consume at their own pace, retry, replay, and fail independently without blocking post creation.

In production, we usually avoid a risky dual-write:

```text
write post row
publish Kafka event
```

If the DB write succeeds but the Kafka publish fails, downstream systems miss the event.

So a common pattern is:

```text
1. write post row inside the database transaction
2. write an outbox row or use CDC
3. publish post_created to Kafka from that durable DB change
```

That is why the diagram shows the database change flowing into Kafka.

### 5. Why Is The Kafka Key `author_user_id`?

**Question: why partition the `post_created` topic by user ID?**

Kafka keeps order within a partition.

The partition key decides which partition an event goes to:

```text
partition = hash(key) % partition_count
```

For a `post_created` topic, `author_user_id` is a good default key because it gives us:

```text
all posts by the same author go to the same partition
events for one author stay ordered
traffic spreads across partitions by author
```

That helps downstream consumers reason about one author's post stream.

But this is not a universal rule.

Choose the Kafka key based on the ordering or grouping you need:

| Topic | Good key | Why |
| --- | --- | --- |
| `post_created` | `author_user_id` | Keep one author's post events ordered. |
| `feed_write` | `viewer_user_id` | Keep feed updates for one viewer ordered. |
| `image_processed` | `image_id` | Keep image lifecycle events ordered. |
| `notification_send` | `recipient_user_id` | Keep one recipient's notifications grouped. |

So the rule is:

```text
partition by the entity whose event order matters
```

## Planned Build Order

1. Upload a photo.
2. Store photo metadata separately from image bytes.
3. Serve photos through a CDN.
4. Generate feeds from followed users.
5. Add hashtag indexing.
6. Scale reads, writes, and search independently.
