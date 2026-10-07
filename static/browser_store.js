"use strict";

const BrowserStore = (() => {
  const DB_NAME = "quant-agent-local";
  const DB_VERSION = 1;
  const STORE = "kv";
  const KEY_ID = "device-encryption-key";
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) {
          req.result.createObjectStore(STORE);
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("无法打开浏览器本地数据库"));
    });
  }

  async function transact(mode, action) {
    const db = await openDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const store = tx.objectStore(STORE);
        let result;
        try { result = action(store); }
        catch (error) { reject(error); return; }
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error || new Error("浏览器本地数据操作失败"));
        tx.onabort = () => reject(tx.error || new Error("浏览器本地数据操作已中止"));
      });
    } finally {
      db.close();
    }
  }

  async function get(key, fallback = null) {
    const db = await openDb();
    try {
      return await new Promise((resolve, reject) => {
        const req = db.transaction(STORE, "readonly").objectStore(STORE).get(key);
        req.onsuccess = () => resolve(req.result === undefined ? fallback : req.result);
        req.onerror = () => reject(req.error || new Error("读取浏览器本地数据失败"));
      });
    } finally {
      db.close();
    }
  }

  async function set(key, value) {
    return transact("readwrite", store => store.put(value, key));
  }

  async function remove(key) {
    return transact("readwrite", store => store.delete(key));
  }

  function toBase64(bytes) {
    let binary = "";
    for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
    return btoa(binary);
  }

  function fromBase64(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  async function getOrCreateCryptoKey() {
    let key = await get(KEY_ID);
    if (key) return key;
    key = await crypto.subtle.generateKey(
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
    await set(KEY_ID, key);
    return key;
  }

  async function encryptSecret(value) {
    if (!value) return null;
    const key = await getOrCreateCryptoKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      encoder.encode(value),
    );
    return { version: 1, iv: toBase64(iv), ciphertext: toBase64(ciphertext) };
  }

  async function decryptSecret(payload) {
    if (!payload) return "";
    const key = await get(KEY_ID);
    if (!key) throw new Error("本机加密密钥已丢失，请重新填写 API Key");
    const clear = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: fromBase64(payload.iv) },
      key,
      fromBase64(payload.ciphertext),
    );
    return decoder.decode(clear);
  }

  async function saveProfiles(profiles, activeProfile = "") {
    const encrypted = [];
    for (const profile of profiles) {
      encrypted.push({
        name: profile.name,
        group: profile.group || "默认组",
        base_url: profile.base_url || "",
        model: profile.model || "",
        encrypted_key: profile.encrypted_key || await encryptSecret(profile.api_key || ""),
      });
    }
    await set("ai-config", { version: 1, active_profile: activeProfile, profiles: encrypted });
  }

  async function loadProfiles() {
    const saved = await get("ai-config", { version: 1, active_profile: "", profiles: [] });
    const profiles = [];
    for (const profile of saved.profiles || []) {
      let apiKey = "";
      try { apiKey = await decryptSecret(profile.encrypted_key); }
      catch (error) { console.warn("API Key 自动解锁失败", error); }
      profiles.push({ ...profile, api_key: apiKey, ready: Boolean(profile.base_url && profile.model && apiKey) });
    }
    return { active_profile: saved.active_profile || profiles[0]?.name || "", profiles };
  }

  return { get, set, remove, saveProfiles, loadProfiles, encryptSecret };
})();
