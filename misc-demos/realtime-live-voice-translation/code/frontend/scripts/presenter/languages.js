(() => {
    const app = window.PresenterApp;
    const { refs, shared } = app;

    const languageMap = new Map(shared.LANGUAGE_OPTIONS.map((language) => [language.code, language]));
    const languagePickers = {
        src: {
            input: refs.srcLangEl,
            field: refs.srcLangFieldEl,
            trigger: refs.srcLangTriggerEl,
            panel: refs.srcLangPickerEl,
            search: refs.srcLangSearchEl,
            list: refs.srcLangListEl,
            empty: refs.srcLangEmptyEl
        },
        tgt: {
            input: refs.tgtLangEl,
            field: refs.tgtLangFieldEl,
            trigger: refs.tgtLangTriggerEl,
            panel: refs.tgtLangPickerEl,
            search: refs.tgtLangSearchEl,
            list: refs.tgtLangListEl,
            empty: refs.tgtLangEmptyEl
        }
    };

    function normalizeSearchText(text) {
        return (text || "")
            .toLowerCase()
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .trim();
    }

    function getLanguageOption(code) {
        return languageMap.get(code) || {
            code: code || "",
            label: code || "Unknown",
            nativeLabel: code || "Unknown",
            popular: false,
            rtl: false
        };
    }

    function languageMetaText(language) {
        return `${language.nativeLabel} · ${language.code.toUpperCase()}`;
    }

    function updateLanguageTrigger(role) {
        const picker = languagePickers[role];
        const language = getLanguageOption(picker.input.value);
        const labelEl = picker.trigger.querySelector("[data-language-label]");
        const metaEl = picker.trigger.querySelector("[data-language-meta]");
        if (labelEl) labelEl.textContent = language.label;
        if (metaEl) metaEl.textContent = languageMetaText(language);
    }

    function renderLanguageOption(language, selectedCode) {
        const selected = language.code === selectedCode;
        const rtlClass = language.rtl ? " is-rtl" : "";
        return `
            <button class="language-option${rtlClass}" type="button" data-code="${language.code}"
                aria-selected="${selected ? "true" : "false"}">
                <span class="language-option-main">
                    <span class="language-option-label">${shared.escapeHtml(language.label)}</span>
                    <span class="language-option-native">${shared.escapeHtml(language.nativeLabel)}</span>
                </span>
                <span class="language-option-code">${shared.escapeHtml(language.code)}</span>
            </button>
        `;
    }

    function renderLanguagePicker(role) {
        const picker = languagePickers[role];
        const selectedCode = picker.input.value;
        const query = normalizeSearchText(picker.search.value);
        const filtered = shared.LANGUAGE_OPTIONS.filter((language) => {
            if (!query) return true;
            const haystack = normalizeSearchText(`${language.label} ${language.nativeLabel} ${language.code}`);
            return haystack.includes(query);
        });

        picker.list.innerHTML = filtered.map((language) => renderLanguageOption(language, selectedCode)).join("");
        picker.empty.hidden = filtered.length > 0;
    }

    function closeLanguagePicker(role, { focusTrigger = false } = {}) {
        const picker = languagePickers[role];
        picker.panel.hidden = true;
        picker.trigger.setAttribute("aria-expanded", "false");
        picker.search.value = "";
        renderLanguagePicker(role);
        if (focusTrigger) picker.trigger.focus();
    }

    function closeAllLanguagePickers({ except = null, focusTrigger = false } = {}) {
        Object.keys(languagePickers).forEach((role) => {
            if (role === except) return;
            closeLanguagePicker(role, { focusTrigger: focusTrigger && role !== except });
        });
    }

    function openLanguagePicker(role) {
        const picker = languagePickers[role];
        closeAllLanguagePickers({ except: role });
        renderLanguagePicker(role);
        picker.panel.hidden = false;
        picker.trigger.setAttribute("aria-expanded", "true");
        picker.search.focus();
        picker.search.select();
    }

    function toggleLanguagePicker(role) {
        const picker = languagePickers[role];
        if (picker.panel.hidden) {
            openLanguagePicker(role);
            return;
        }
        closeLanguagePicker(role, { focusTrigger: true });
    }

    function applyLanguagePair(nextSrc, nextTgt, { emit = false, emitRole = "src" } = {}) {
        if (languageMap.has(nextSrc)) refs.srcLangEl.value = nextSrc;
        if (languageMap.has(nextTgt)) refs.tgtLangEl.value = nextTgt;

        updateLanguageTrigger("src");
        updateLanguageTrigger("tgt");
        renderLanguagePicker("src");
        renderLanguagePicker("tgt");

        if (emit) {
            languagePickers[emitRole].input.dispatchEvent(new Event("change", { bubbles: true }));
        }
    }

    function selectLanguage(role, code) {
        if (!languageMap.has(code)) return;

        const currentSrc = refs.srcLangEl.value;
        const currentTgt = refs.tgtLangEl.value;
        let nextSrc = currentSrc;
        let nextTgt = currentTgt;

        if (role === "src") {
            if (code === currentTgt && code !== currentSrc) {
                nextSrc = code;
                nextTgt = currentSrc;
            } else {
                nextSrc = code;
            }
        } else {
            if (code === currentSrc && code !== currentTgt) {
                nextSrc = currentTgt;
                nextTgt = code;
            } else {
                nextTgt = code;
            }
        }

        applyLanguagePair(nextSrc, nextTgt, { emit: true, emitRole: role });
        closeLanguagePicker(role, { focusTrigger: true });
    }

    function visibleLanguageButtons(role) {
        return Array.from(languagePickers[role].list.querySelectorAll("button.language-option"));
    }

    function focusLanguageButton(role, direction, currentButton = null) {
        const buttons = visibleLanguageButtons(role);
        if (buttons.length === 0) return;
        const currentIndex = currentButton ? buttons.indexOf(currentButton) : -1;
        const nextIndex = currentIndex === -1
            ? (direction > 0 ? 0 : buttons.length - 1)
            : (currentIndex + direction + buttons.length) % buttons.length;
        buttons[nextIndex].focus();
    }

    function handleLanguageListClick(role, event) {
        const button = event.target.closest("button[data-code]");
        if (!button) return;
        selectLanguage(role, button.dataset.code || "");
    }

    function handleLanguageListKeydown(role, event) {
        const button = event.target.closest("button[data-code]");
        if (!button) return;
        if (event.key === "ArrowDown") {
            event.preventDefault();
            focusLanguageButton(role, 1, button);
        } else if (event.key === "ArrowUp") {
            event.preventDefault();
            focusLanguageButton(role, -1, button);
        } else if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectLanguage(role, button.dataset.code || "");
        }
    }

    function initializeLanguagePickers() {
        Object.entries(languagePickers).forEach(([role, picker]) => {
            updateLanguageTrigger(role);
            renderLanguagePicker(role);

            picker.trigger.addEventListener("click", () => toggleLanguagePicker(role));
            picker.trigger.addEventListener("keydown", (event) => {
                if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openLanguagePicker(role);
                }
            });

            picker.search.addEventListener("input", () => renderLanguagePicker(role));
            picker.search.addEventListener("keydown", (event) => {
                if (event.key === "ArrowDown") {
                    event.preventDefault();
                    focusLanguageButton(role, 1);
                } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    focusLanguageButton(role, -1);
                } else if (event.key === "Escape") {
                    event.preventDefault();
                    closeLanguagePicker(role, { focusTrigger: true });
                }
            });

            picker.list.addEventListener("click", (event) => handleLanguageListClick(role, event));
            picker.list.addEventListener("keydown", (event) => handleLanguageListKeydown(role, event));
        });

        document.addEventListener("click", (event) => {
            const clickedInsidePicker = Object.values(languagePickers).some((picker) => picker.field.contains(event.target));
            if (!clickedInsidePicker) closeAllLanguagePickers();
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            const hasOpenPicker = Object.values(languagePickers).some((picker) => !picker.panel.hidden);
            if (hasOpenPicker) closeAllLanguagePickers();
        });
    }

    Object.assign(app, {
        languageMap,
        languagePickers,
        applyLanguagePair,
        initializeLanguagePickers
    });
})();
