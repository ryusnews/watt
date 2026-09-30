-- 채팅 글꼴을 한글·중국어(간체/번체)·러시아어·영어를 모두 담은 Sarasa Gothic K 로 바꾼다.
-- WoW 는 글꼴에 없는 글자를 다른 글꼴로 대신 그리지 않아, 한국어 클라이언트에서 중국어가 □ 로 보인다.
--
-- 적용 대상
--  1) 블리자드 채팅창(ChatFrameN) — EllesmereUI Chat 이 없을 때 보이는 창.
--  2) EllesmereUI Chat — 블리자드 창을 숨기고 자기 창을 폰트 패밀리(EUIChatFontFamily<id>)로 그린다.
--     그 패밀리의 한국어·중국어 간체·번체 글꼴만 Sarasa 로 바꾼다(라틴·러시아어는 사용자가 고른 글꼴 그대로).
--     EllesmereUI 가 글꼴·크기를 바꾸며 되돌리면 1초 주기 확인에서 다시 맞춘다(이미 맞으면 아무것도 안 함).
--  3) LibSharedMedia 에 등록 — EllesmereUI 등 글꼴 목록에 "Sarasa Gothic K (CJK)" 로 나온다.
-- /cjkfont 상태 · /cjkfont on|off (끈 뒤 원래 글꼴은 /reload)
local ADDON = ...
local FONT = "Interface\\AddOns\\" .. ADDON .. "\\Fonts\\SarasaGothicK-Regular.ttf"
local LSM_NAME = "Sarasa Gothic K (CJK)"
local CJK_ALPHABETS = { "korean", "simplifiedchinese", "traditionalchinese" }

local function apply(obj)
    if not obj or not obj.GetFont then return false end
    local file, size, flags = obj:GetFont()
    if size and file ~= FONT then
        obj:SetFont(FONT, size, flags or "")
        return true
    end
    return false
end

local function chatFrameNames()
    if CHAT_FRAMES then return CHAT_FRAMES end  -- 귓속말 창 같은 임시 창까지 포함
    local names = {}
    for i = 1, NUM_CHAT_WINDOWS do names[#names + 1] = "ChatFrame" .. i end
    return names
end

local function applyBlizzard()
    apply(ChatFontNormal)
    apply(ChatFontSmall)
    for _, name in ipairs(chatFrameNames()) do
        apply(_G[name])
        apply(_G[name .. "EditBox"])  -- 입력창도
    end
end

-- EllesmereUI Chat 폰트 패밀리 -------------------------------------------------
local euiFamilies = {}   -- 찾은 패밀리(이름 → 객체)

local function patchFamily(fam)
    if not fam or not fam.GetFontObjectForAlphabet then return 0 end
    local changed = 0
    for _, alphabet in ipairs(CJK_ALPHABETS) do
        local ok, member = pcall(fam.GetFontObjectForAlphabet, fam, alphabet)
        if ok and member and apply(member) then changed = changed + 1 end
    end
    return changed
end

local function findFamilies(scanFrames)
    for id = 1, 30 do
        local name = "EUIChatFontFamily" .. id
        local fam = _G[name]
        if fam then euiFamilies[name] = fam end
    end
    -- 전역 이름으로 안 잡히면 화면의 메시지 창에서 패밀리를 찾는다(가끔만)
    if scanFrames and not next(euiFamilies) and EnumerateFrames then
        local f = EnumerateFrames()
        while f do
            if f.GetObjectType and f:GetObjectType() == "ScrollingMessageFrame" and f.GetFontObject then
                local fo = f:GetFontObject()
                local n = fo and fo.GetName and fo:GetName()
                if n and n:find("^EUIChatFontFamily") then euiFamilies[n] = fo end
            end
            f = EnumerateFrames(f)
        end
    end
end

local function patchEllesmere(scanFrames)
    findFamilies(scanFrames)
    local changed = 0
    for _, fam in pairs(euiFamilies) do changed = changed + patchFamily(fam) end
    return changed
end

local function applyAll(scanFrames)
    if not ChatFontCJKDB or not ChatFontCJKDB.enabled then return end
    applyBlizzard()
    patchEllesmere(scanFrames)
end

-- LibSharedMedia 등록 -----------------------------------------------------------
local function registerLSM()
    local LSM = LibStub and LibStub("LibSharedMedia-3.0", true)
    if not LSM then return false end
    local mask
    if bit and LSM.LOCALE_BIT_koKR then
        mask = bit.bor(LSM.LOCALE_BIT_koKR, LSM.LOCALE_BIT_zhCN or 0, LSM.LOCALE_BIT_zhTW or 0,
            LSM.LOCALE_BIT_ruRU or 0, LSM.LOCALE_BIT_western or 0)
    end
    LSM:Register("font", LSM_NAME, FONT, mask)
    return true
end

local ticker
local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("PLAYER_ENTERING_WORLD")
f:RegisterEvent("UPDATE_CHAT_WINDOWS")
f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 ~= ADDON then return end
        ChatFontCJKDB = ChatFontCJKDB or { enabled = true }
        registerLSM()
        if FCF_OpenTemporaryWindow then hooksecurefunc("FCF_OpenTemporaryWindow", function() applyAll(false) end) end
        if FCF_SetChatWindowFontSize then hooksecurefunc("FCF_SetChatWindowFontSize", function() applyAll(false) end) end
        return
    end
    applyAll(true)
    if not ticker and C_Timer and C_Timer.NewTicker then
        local n = 0
        ticker = C_Timer.NewTicker(1, function()
            n = n + 1
            applyAll(n % 10 == 0)  -- 창 훑기는 10초에 한 번만
        end)
    end
end)

SLASH_CHATFONTCJK1 = "/cjkfont"
SlashCmdList.CHATFONTCJK = function(msg)
    msg = strlower(strtrim(msg or ""))
    if msg == "on" then
        ChatFontCJKDB.enabled = true
        applyAll(true)
    elseif msg == "off" then
        ChatFontCJKDB.enabled = false
    end
    findFamilies(true)
    local fams, ok = 0, 0
    for _, fam in pairs(euiFamilies) do
        fams = fams + 1
        local member = fam:GetFontObjectForAlphabet("simplifiedchinese")
        if member and member:GetFont() == FONT then ok = ok + 1 end
    end
    local blizz = ChatFrame1:GetFont() == FONT
    print(("|cff33ff99ChatFontCJK|r %s — 블리자드 채팅창: %s · EllesmereUI 채팅 패밀리: %d개 중 %d개 적용"):format(
        ChatFontCJKDB.enabled and "켜짐" or "꺼짐", blizz and "적용" or "미적용", fams, ok))
    if not ChatFontCJKDB.enabled then print("  원래 글꼴로 돌아가려면 /reload") end
end
