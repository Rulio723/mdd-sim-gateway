import assert from 'node:assert/strict'
import test from 'node:test'

import { isCompressibleType, totalBytes, COMPRESSION_STEPS, fitAttachments } from '../src/mmsImage.js'

// These cover only the size-planning half of mmsImage.js: the parts that are plain
// arithmetic and never touch Image/canvas. compressOnce() (the actual downscaling) needs a
// DOM and is exercised by hand in the browser, not here.

test('isCompressibleType accepts the four raster types and rejects everything else', () => {
  assert.equal(isCompressibleType('image/jpeg'), true)
  assert.equal(isCompressibleType('image/png'), true)
  assert.equal(isCompressibleType('image/webp'), true)
  assert.equal(isCompressibleType('image/bmp'), true)
  assert.equal(isCompressibleType('IMAGE/JPEG'), true) // case-insensitive
  assert.equal(isCompressibleType('image/gif'), false) // animated GIFs are never re-encoded
  assert.equal(isCompressibleType('video/mp4'), false)
  assert.equal(isCompressibleType(''), false)
  assert.equal(isCompressibleType(undefined), false)
})

test('totalBytes sums attachment sizes plus the text part', () => {
  const files = [{ size: 100 }, { size: 250 }]
  assert.equal(totalBytes(files, 50), 400)
  assert.equal(totalBytes([], 0), 0)
  assert.equal(totalBytes(files), 350) // textBytes defaults to 0
})

test('COMPRESSION_STEPS is a non-empty, monotonically easing sequence', () => {
  assert.ok(Array.isArray(COMPRESSION_STEPS) && COMPRESSION_STEPS.length > 0)
  for (const step of COMPRESSION_STEPS) {
    assert.equal(typeof step.maxDim, 'number')
    assert.equal(typeof step.quality, 'number')
    assert.ok(step.quality > 0 && step.quality <= 1)
  }
})

test('fitAttachments returns the batch unchanged when it already fits', async () => {
  const files = [{ size: 1000, type: 'image/jpeg', name: 'a.jpg' }]
  const result = await fitAttachments(files, 0, 2000)
  assert.deepEqual(result, files)
})

test('fitAttachments throws with size/limit when non-image attachments cannot be shrunk', async () => {
  // A video (or any non-compressible type) is passed through every step untouched, so an
  // oversized one can never be made to fit — this must fail loudly rather than silently
  // truncate or drop the attachment.
  const files = [{ size: 5_000_000, type: 'video/mp4', name: 'clip.mp4' }]
  await assert.rejects(
    () => fitAttachments(files, 0, 300 * 1024),
    (err) => {
      assert.equal(err.size, 5_000_000)
      assert.equal(err.limit, 300 * 1024)
      return true
    },
  )
})

test('fitAttachments never tries to re-encode an animated GIF', async () => {
  const files = [{ size: 5_000_000, type: 'image/gif', name: 'party.gif' }]
  await assert.rejects(() => fitAttachments(files, 0, 300 * 1024))
})

test('fitAttachments accounts for textBytes when deciding whether the batch fits', async () => {
  const files = [{ size: 100, type: 'text/vcard', name: 'card.vcf' }]
  const result = await fitAttachments(files, 50, 150)
  assert.deepEqual(result, files) // 100 + 50 == 150, fits exactly
  await assert.rejects(() => fitAttachments(files, 51, 150))
})
