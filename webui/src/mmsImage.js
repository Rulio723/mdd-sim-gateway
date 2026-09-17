// Client-side attachment sizing for MMS. There is no server-side transcoding step: a photo
// straight off a phone camera is routinely 3-8 MB, while a line's MMS limit is commonly
// 300 KB-1 MB and the modem transport uploads at roughly 200 bytes/second (see
// control/app/mms_transport.py). Shrinking oversized images here, before the multipart
// upload ever starts, is what keeps a normal photo attachment from taking minutes or being
// rejected outright by the MMSC.
//
// The size-planning half of this module (isCompressibleType, totalBytes, COMPRESSION_STEPS,
// and the early-exit / exhausted-steps paths of fitAttachments) is plain arithmetic and is
// covered by webui/tests/mms-image.test.mjs without a DOM. Only compressOnce() touches
// browser-only APIs (Image, canvas, URL.createObjectURL) and only runs for attachments that
// are both compressible and still over budget.

const COMPRESSIBLE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/bmp'])

// Applied in order until the batch fits. The first step is a mild resize at good quality;
// later steps trade more size for more quality loss. Numbers are deliberately approximate
// ("e.g.") — this is a best-effort fit, not an exact target.
export const COMPRESSION_STEPS = [
  { maxDim: 1280, quality: 0.82 },
  { maxDim: 1024, quality: 0.7 },
  { maxDim: 800, quality: 0.6 },
  { maxDim: 640, quality: 0.5 },
]

export function isCompressibleType(type) {
  return COMPRESSIBLE_TYPES.has(String(type || '').toLowerCase())
}

/** Total bytes a send would carry: every attachment's size plus the text part. Multipart
 * framing overhead is a few hundred bytes per part and is not worth modelling here. */
export function totalBytes(files, textBytes = 0) {
  return files.reduce((sum, f) => sum + (f && f.size || 0), 0) + Number(textBytes || 0)
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => resolve({ img, url })
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('could not decode image')) }
    img.src = url
  })
}

function canvasToJpegBlob(canvas, quality) {
  return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), 'image/jpeg', quality))
}

async function compressOnce(file, maxDim, quality) {
  const { img, url } = await loadImage(file)
  try {
    let { naturalWidth: width, naturalHeight: height } = img
    if (!width || !height) return file
    if (width > maxDim || height > maxDim) {
      const scale = maxDim / Math.max(width, height)
      width = Math.max(1, Math.round(width * scale))
      height = Math.max(1, Math.round(height * scale))
    }
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    ctx.drawImage(img, 0, 0, width, height)
    const blob = await canvasToJpegBlob(canvas, quality)
    if (!blob) return file
    const name = String(file.name || 'image').replace(/\.\w+$/, '') + '.jpg'
    return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() })
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** Downscale `files` step by step so their total size (plus `textBytes`) fits `maxSize`.
 * Animated GIFs and non-image files are never touched — a GIF loses its animation if drawn
 * through a canvas, and other types cannot be re-encoded as an image at all. Returns the
 * (possibly unchanged) files as soon as they fit, or throws an Error with `.size` and
 * `.limit` set (bytes) once every step has been tried and the batch still does not fit. */
export async function fitAttachments(files, textBytes, maxSize) {
  const originals = Array.from(files || [])
  const limit = Number(maxSize) || 0
  if (totalBytes(originals, textBytes) <= limit) return originals
  let current = originals
  for (const step of COMPRESSION_STEPS) {
    current = await Promise.all(originals.map((f) => (
      isCompressibleType(f && f.type) ? compressOnce(f, step.maxDim, step.quality) : f
    )))
    if (totalBytes(current, textBytes) <= limit) return current
  }
  const size = totalBytes(current, textBytes)
  const err = new Error(`attachments too large: ${size} bytes, limit ${limit} bytes`)
  err.size = size
  err.limit = limit
  throw err
}
