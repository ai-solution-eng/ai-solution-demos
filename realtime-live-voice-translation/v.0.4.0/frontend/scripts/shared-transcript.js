(() => {
    const shared = window.RealtimeTranslationShared;

    function toExportTranscriptItem(item, fallbackTargetLanguage = "") {
        return {
            original: item?.original || "",
            translation: item?.translation || "",
            src: item?.src || "",
            tgt: item?.tgt || fallbackTargetLanguage || "",
            ts_ms: item?.ts_ms || null
        };
    }

    function finalizedTranscriptItems(items = [], fallbackTargetLanguage = "") {
        return items
            .filter((item) => item?.is_final && ((item.original || "").trim() || (item.translation || "").trim()))
            .map((item) => toExportTranscriptItem(item, fallbackTargetLanguage));
    }

    function upsertSegment(list, segment) {
        const index = list.findIndex((item) => item.segment_id === segment.segment_id);
        if (index === -1) {
            list.push(segment);
            return segment;
        }
        list[index] = { ...list[index], ...segment };
        return list[index];
    }

    function resolveVisibleTranscriptItems(items = [], finalHoldMs = 0, nowMs = Date.now()) {
        if (items.length === 0 || finalHoldMs <= 0) {
            return { visibleItems: items, refreshAtMs: null };
        }

        const latestItem = items[items.length - 1];
        if (latestItem?.is_final) {
            return { visibleItems: items, refreshAtMs: null };
        }

        for (let index = items.length - 2; index >= 0; index -= 1) {
            const candidate = items[index];
            if (!candidate?.is_final) continue;

            const finalizedAtMs = Number(candidate.finalized_at_ms || 0);
            if (!finalizedAtMs) {
                return { visibleItems: items, refreshAtMs: null };
            }

            const refreshAtMs = finalizedAtMs + finalHoldMs;
            if (refreshAtMs > nowMs) {
                return {
                    visibleItems: items.slice(0, index + 1),
                    refreshAtMs
                };
            }

            return { visibleItems: items, refreshAtMs: null };
        }

        return { visibleItems: items, refreshAtMs: null };
    }

    function replaceSegments(target, segments = []) {
        target.length = 0;
        segments.forEach((segment) => target.push({ ...segment }));
        return target;
    }

    function upsertSegmentFactCheck(list, payload) {
        const index = list.findIndex((item) => item.segment_id === payload.segment_id);
        if (index === -1) return null;
        const current = list[index] || {};
        const currentRevision = Number(current.revision || 0) || 0;
        const incomingRevision = Number(payload.revision || 0) || 0;
        if (incomingRevision && currentRevision && incomingRevision < currentRevision) {
            return current;
        }
        list[index] = { ...current, fact_check: payload.fact_check || {} };
        return list[index];
    }

    function flaggedFactCheckItems(items = [], fallbackTargetLanguage = "") {
        return items
            .filter((item) => shared.factCheckIsFlagged(item?.fact_check))
            .slice()
            .sort((left, right) => Number(right?.ts_ms || 0) - Number(left?.ts_ms || 0))
            .map((item) => ({
                ...item,
                tgt: item?.tgt || fallbackTargetLanguage || "",
                fact_check: item?.fact_check || {}
            }));
    }

    function buildFactCheckSources(sources = []) {
        if (!Array.isArray(sources) || sources.length === 0) return "";
        return `
            <div class="review-sources">
                ${sources.map((source) => {
                    const title = shared.escapeHtml(source?.title || source?.url || "Source");
                    const url = shared.escapeHtml(source?.url || "");
                    const snippet = shared.escapeHtml(source?.snippet || "");
                    const link = url
                        ? `<a class="review-source-link" href="${url}" target="_blank" rel="noreferrer">${title}</a>`
                        : `<span class="review-source-link">${title}</span>`;
                    const snippetHtml = snippet ? `<p class="review-source-snippet">${snippet}</p>` : "";
                    return `<article class="review-source">${link}${snippetHtml}</article>`;
                }).join("")}
            </div>
        `;
    }

    function renderFactCheckPanel({ items = [], refs, targetLanguage = "" }) {
        if (!refs.factCheckPanelEl || !refs.factCheckListEl || !refs.factCheckCountEl) return;
        const flaggedItems = flaggedFactCheckItems(items, targetLanguage);
        refs.factCheckPanelEl.hidden = flaggedItems.length === 0;
        refs.factCheckCountEl.textContent = `${flaggedItems.length} flagged`;
        if (flaggedItems.length === 0) {
            refs.factCheckListEl.innerHTML = "";
            refs.factCheckPanelEl.open = false;
            return;
        }
        refs.factCheckListEl.innerHTML = flaggedItems.map((item) => {
            const factCheck = item.fact_check || {};
            const status = shared.factCheckLabel(factCheck.status);
            const statusClass = shared.factCheckToneClass(factCheck.status);
            const original = shared.escapeHtml(item.original || "");
            const translation = shared.escapeHtml(item.translation || "");
            const motivation = shared.escapeHtml(factCheck.motivation || "Review requires more evidence.");
            const provider = shared.escapeHtml((factCheck.provider || "llm").toUpperCase());
            const timeLabel = shared.escapeHtml(shared.formatTurnTime(item.ts_ms));
            const sources = buildFactCheckSources(factCheck.sources || []);
            const translationBlock = translation
                ? `
                    <div class="review-copy-label">Translation</div>
                    <p class="review-copy review-translation">${translation}</p>
                `
                : "";
            return `
                <article class="review-item ${statusClass}">
                    <div class="review-item-meta">
                        <div class="review-tags">
                            <span class="review-status-chip ${statusClass}">${status}</span>
                            <span class="review-provider-chip">${provider}</span>
                        </div>
                        <span class="review-time">${timeLabel}</span>
                    </div>
                    <div class="review-copy-label">Statement</div>
                    <p class="review-copy">${original}</p>
                    ${translationBlock}
                    <div class="review-copy-label">Why it was flagged</div>
                    <p class="review-copy review-motivation">${motivation}</p>
                    ${sources}
                </article>
            `;
        }).join("");
    }

    function buildLiveCard(item, { variant, turnNumber, targetLanguage = "", timeSeparator = " - " }) {
        const timeLabel = shared.formatTurnTime(item.ts_ms);
        const original = shared.escapeHtml(item.original || "Awaiting source speech.");
        const translation = shared.escapeHtml(item.translation || "Awaiting translated output.");
        const route = `${shared.escapeHtml(shared.languageName(item.src))} -> ${shared.escapeHtml(shared.languageName(item.tgt || targetLanguage))}`;
        const suffix = timeLabel ? `${timeSeparator}${shared.escapeHtml(timeLabel)}` : "";
        const status = shared.escapeHtml(shared.statusLabel(item));
        const statusClass = shared.statusToneClass(item);

        return `
            <article class="pair ${variant}">
                <div class="pair-meta">
                    <div class="pair-tags">
                        <span class="lang-chip">${route}</span>
                        <span class="status-chip ${statusClass}">${status}</span>
                    </div>
                    <span class="pair-label">Turn ${turnNumber}${suffix}</span>
                </div>
                <div class="pair-original-label">Original</div>
                <p class="pair-copy original">${original}</p>
                <div class="pair-translation-label">Translation</div>
                <p class="pair-copy translation">${translation}</p>
            </article>
        `;
    }

    function resolvePlaceholderText(value, targetLanguage) {
        return typeof value === "function" ? value(targetLanguage) : (value || "");
    }

    function renderTranscriptPanels({
        items = [],
        refs,
        targetLanguage = "",
        placeholder,
        timeSeparator = " - ",
        finalHoldMs = 0
    }) {
        const renderState = resolveVisibleTranscriptItems(items, finalHoldMs);
        const visibleItems = renderState.visibleItems;

        if (visibleItems.length === 0) {
            refs.stackEl.innerHTML = `
                <article class="pair placeholder latest">
                    <div>
                        <div class="pair-label">${resolvePlaceholderText(placeholder.label, targetLanguage)}</div>
                        <p class="pair-copy original">${resolvePlaceholderText(placeholder.original, targetLanguage)}</p>
                        <p class="pair-copy translation">${resolvePlaceholderText(placeholder.translation, targetLanguage)}</p>
                    </div>
                </article>
            `;
            refs.historyListEl.innerHTML = "";
            refs.historyCountEl.textContent = "0 earlier turns";
            refs.historyPanelEl.open = false;
            return renderState;
        }

        const liveItems = visibleItems.slice(-1).reverse();
        const olderItems = visibleItems.slice(0, -1).reverse();

        refs.stackEl.innerHTML = liveItems
            .map((item, index) => buildLiveCard(item, {
                variant: index === 0 ? "latest" : "previous",
                turnNumber: visibleItems.length - index,
                targetLanguage,
                timeSeparator
            }))
            .join("");

        refs.historyListEl.innerHTML = olderItems.length === 0
            ? ""
            : olderItems.map((item, index) => `
                <article class="history-item">
                    ${buildLiveCard(item, {
                        variant: "previous",
                        turnNumber: visibleItems.length - 1 - index,
                        targetLanguage,
                        timeSeparator
                    })}
                </article>
            `).join("");

        refs.historyCountEl.textContent = `${olderItems.length} earlier turn${olderItems.length === 1 ? "" : "s"}`;
        if (olderItems.length === 0) refs.historyPanelEl.open = false;
        requestAnimationFrame(() => refs.centerEl.scrollTo({ top: 0 }));
        return renderState;
    }

    function buildTranscriptText(items, which) {
        return items
            .map((item) => {
                const timestamp = item.ts_ms ? `${new Date(item.ts_ms).toISOString()} ` : "";
                const text = (which === "original" ? item.original : item.translation) || "";
                return (timestamp + text).trim();
            })
            .filter(Boolean)
            .join("\n");
    }

    function escapeCsv(value) {
        return (value || "").replace(/"/g, '""');
    }

    function buildParallelCsv(items) {
        let csv = "segment,ts,src,tgt,original,translation\n";
        items.forEach((item, index) => {
            const ts = item.ts_ms ? new Date(item.ts_ms).toISOString() : "";
            csv += `${index + 1},"${ts}","${escapeCsv(item.src)}","${escapeCsv(item.tgt)}","${escapeCsv(item.original)}","${escapeCsv(item.translation)}"\n`;
        });
        return csv;
    }

    window.RealtimeTranslationTranscript = Object.freeze({
        buildParallelCsv,
        buildTranscriptText,
        finalizedTranscriptItems,
        flaggedFactCheckItems,
        resolveVisibleTranscriptItems,
        renderTranscriptPanels,
        renderFactCheckPanel,
        replaceSegments,
        toExportTranscriptItem,
        upsertSegmentFactCheck,
        upsertSegment
    });
})();
