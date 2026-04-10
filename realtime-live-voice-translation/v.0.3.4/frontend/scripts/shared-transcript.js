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

    function replaceSegments(target, segments = []) {
        target.length = 0;
        segments.forEach((segment) => target.push({ ...segment }));
        return target;
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

    function renderTranscriptPanels({ items = [], refs, targetLanguage = "", placeholder, timeSeparator = " - " }) {
        if (items.length === 0) {
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
            return;
        }

        const liveItems = items.slice(-1).reverse();
        const olderItems = items.slice(0, -1).reverse();

        refs.stackEl.innerHTML = liveItems
            .map((item, index) => buildLiveCard(item, {
                variant: index === 0 ? "latest" : "previous",
                turnNumber: items.length - index,
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
                        turnNumber: items.length - 1 - index,
                        targetLanguage,
                        timeSeparator
                    })}
                </article>
            `).join("");

        refs.historyCountEl.textContent = `${olderItems.length} earlier turn${olderItems.length === 1 ? "" : "s"}`;
        if (olderItems.length === 0) refs.historyPanelEl.open = false;
        requestAnimationFrame(() => refs.centerEl.scrollTo({ top: 0 }));
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
        renderTranscriptPanels,
        replaceSegments,
        toExportTranscriptItem,
        upsertSegment
    });
})();
