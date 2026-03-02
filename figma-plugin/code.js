figma.showUI(__html__, {
  width: 780,
  height: 680,
  themeColors: true,
});

function canExport(node) {
  return typeof node.exportAsync === "function";
}

function parseOfferAndSequence(name, fallbackIndex) {
  const trimmed = String(name || "").trim();
  if (!trimmed) {
    return {
      offerId: `unknown_${fallbackIndex}`,
      sequence: fallbackIndex,
      warning: "Имя слоя пустое. Использован временный offer_id.",
    };
  }

  const delimiterIndex = trimmed.lastIndexOf("_");
  if (delimiterIndex > 0 && delimiterIndex < trimmed.length - 1) {
    const offer = trimmed.slice(0, delimiterIndex).trim();
    const seqCandidate = Number(trimmed.slice(delimiterIndex + 1));
    if (offer && Number.isFinite(seqCandidate) && seqCandidate > 0) {
      return { offerId: offer, sequence: seqCandidate, warning: null };
    }
  }

  return {
    offerId: trimmed,
    sequence: fallbackIndex,
    warning: `Имя "${trimmed}" не в формате offer_id_номер. Порядок взят из выделения.`,
  };
}

function bytesToBase64(bytes) {
  if (typeof figma.base64Encode === "function") {
    return figma.base64Encode(bytes);
  }

  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.slice(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function uiLog(stage, message, meta = null, level = "info") {
  figma.ui.postMessage({
    type: "plugin-log",
    stage,
    message,
    meta,
    level,
  });
}

async function getNodeByIdCompat(nodeId) {
  if (!nodeId) {
    return null;
  }
  if (typeof figma.getNodeByIdAsync === "function") {
    try {
      return await figma.getNodeByIdAsync(nodeId);
    } catch (error) {
      uiLog("node", "getNodeByIdAsync failed", { nodeId, error: String(error) }, "error");
      return null;
    }
  }
  try {
    return figma.getNodeById(nodeId);
  } catch (error) {
    uiLog("node", "getNodeById failed", { nodeId, error: String(error) }, "error");
    return null;
  }
}

function readSelectionMetadata() {
  const selection = figma.currentPage.selection.filter(canExport);

  if (!selection.length) {
    return {
      images: [],
      warnings: ["Выделите хотя бы один экспортируемый слой/фрейм."],
    };
  }

  const images = [];
  const warnings = [];

  for (let index = 0; index < selection.length; index += 1) {
    const node = selection[index];
    const parsed = parseOfferAndSequence(node.name, index + 1);
    if (parsed.warning) {
      warnings.push(parsed.warning);
    }

    images.push({
      localId: node.id,
      nodeId: node.id,
      sourceName: node.name,
      offerId: parsed.offerId,
      sequence: parsed.sequence,
      mimeType: "image/png",
      filename: `${parsed.offerId}_${parsed.sequence}.png`,
    });
  }

  images.sort((a, b) => {
    if (a.offerId === b.offerId) {
      return a.sequence - b.sequence;
    }
    return a.offerId.localeCompare(b.offerId, "ru");
  });

  return {
    images,
    warnings,
  };
}

async function sendSelectionMetadata() {
  const payload = readSelectionMetadata();
  uiLog("selection", "Отправка метаданных выделения", {
    images: payload.images.length,
    warnings: payload.warnings.length,
  });
  figma.ui.postMessage({
    type: "selection-data",
    images: payload.images,
    warnings: payload.warnings,
  });
}

async function exportPreviewImages(requestId, items) {
  const safeItems = Array.isArray(items) ? items : [];
  let successCount = 0;
  const errors = [];
  uiLog("preview", "Старт экспорта превью", { requestId, count: safeItems.length });

  for (let index = 0; index < safeItems.length; index += 1) {
    const item = safeItems[index];
    const nodeId = item && typeof item.nodeId === "string" ? item.nodeId : "";
    const localId = item && typeof item.localId === "string" ? item.localId : nodeId;

    if (!nodeId || !localId) {
      continue;
    }

    const node = await getNodeByIdCompat(nodeId);
    if (!node || !canExport(node)) {
      errors.push(`Нода не найдена для preview: ${nodeId}`);
      continue;
    }

    try {
      const bytes = await node.exportAsync({
        format: "PNG",
        constraint: { type: "WIDTH", value: 120 },
      });
      const base64 = bytesToBase64(bytes);
      successCount += 1;
      figma.ui.postMessage({
        type: "preview-chunk",
        requestId,
        previews: [{ localId, dataUrl: `data:image/png;base64,${base64}` }],
        errors: [],
      });
    } catch (error) {
      errors.push(`Не удалось сделать preview для ${node.name || nodeId}: ${String(error)}`);
    }

    figma.ui.postMessage({
      type: "preview-progress",
      requestId,
      done: index + 1,
      total: safeItems.length,
    });
  }

  figma.ui.postMessage({
    type: "preview-result",
    requestId,
    previews: [],
    errors,
  });
  uiLog("preview", "Экспорт превью завершен", {
    requestId,
    total: safeItems.length,
    success: successCount,
    errors: errors.length,
  });
}

async function exportImagesForSync(requestId, items) {
  const safeItems = Array.isArray(items) ? items : [];
  const total = safeItems.length;
  let successCount = 0;
  const errors = [];
  uiLog("export", "Старт экспорта изображений", { requestId, count: total });

  for (let index = 0; index < safeItems.length; index += 1) {
    const item = safeItems[index];
    const nodeId = item && typeof item.nodeId === "string" ? item.nodeId : "";
    const localId = item && typeof item.localId === "string" ? item.localId : nodeId;
    const filename = item && typeof item.filename === "string" ? item.filename : `${nodeId}.png`;

    if (!nodeId) {
      errors.push("Пропущен nodeId при экспорте изображения");
      continue;
    }

    const node = await getNodeByIdCompat(nodeId);
    if (!node || !canExport(node)) {
      errors.push(`Нода не найдена или не экспортируется: ${nodeId}`);
      continue;
    }

    try {
      const bytes = await node.exportAsync({ format: "PNG" });
      successCount += 1;
      figma.ui.postMessage({
        type: "export-image-chunk",
        requestId,
        images: [
          {
            localId,
            nodeId,
            filename,
            mimeType: "image/png",
            contentBase64: bytesToBase64(bytes),
          },
        ],
        errors: [],
      });
    } catch (error) {
      errors.push(`Не удалось экспортировать ${node.name || nodeId}: ${String(error)}`);
    }

    figma.ui.postMessage({
      type: "export-images-progress",
      requestId,
      done: index + 1,
      total,
    });
  }

  figma.ui.postMessage({
    type: "export-images-result",
    requestId,
    images: [],
    errors,
  });
  uiLog("export", "Экспорт изображений завершен", {
    requestId,
    total,
    success: successCount,
    errors: errors.length,
  });
}

figma.ui.onmessage = async (message) => {
  if (!message || typeof message !== "object") {
    return;
  }

  try {
    if (message.type === "request-selection") {
      await sendSelectionMetadata();
      return;
    }

    if (message.type === "request-previews") {
      await exportPreviewImages(message.requestId, message.items);
      return;
    }

    if (message.type === "export-images") {
      await exportImagesForSync(message.requestId, message.items);
      return;
    }

    if (message.type === "notify") {
      figma.notify(String(message.message || "Готово"), {
        error: Boolean(message.error),
      });
      return;
    }

    if (message.type === "close") {
      figma.closePlugin();
    }
  } catch (error) {
    uiLog("code", "Необработанная ошибка в обработчике message", { error: String(error) }, "error");
    throw error;
  }
};
