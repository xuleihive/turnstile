const supportedTypes = new Set(["image/png", "image/jpeg", "image/webp"])

export async function prepareAvatar(file: File): Promise<string> {
  if (!supportedTypes.has(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片。")
  if (file.size > 10 * 1024 * 1024) throw new Error("原始图片不能超过 10 MB。")
  const image = await createImageBitmap(file).catch(() => {
    throw new Error("无法读取这张图片。")
  })
  try {
    const size = Math.min(image.width, image.height)
    if (!size) throw new Error("图片尺寸无效。")
    for (const outputSize of [192, 160, 128]) {
      const canvas = document.createElement("canvas")
      canvas.width = canvas.height = outputSize
      const context = canvas.getContext("2d")
      if (!context) throw new Error("浏览器无法处理这张图片。")
      context.drawImage(image, (image.width - size) / 2, (image.height - size) / 2,
        size, size, 0, 0, outputSize, outputSize)
      for (const quality of [0.82, 0.68, 0.54, 0.4]) {
        const candidate = canvas.toDataURL("image/webp", quality)
        const encoded = candidate.slice(candidate.indexOf(",") + 1)
        const padding = encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0
        if (Math.floor(encoded.length * 3 / 4) - padding <= 64 * 1024) return candidate
      }
    }
    throw new Error("压缩后的头像仍然过大，请选择更简单的图片。")
  } finally {
    image.close()
  }
}
