import { ApiError } from './api';
import { savePair, type MobilePair, type RelayDevice } from './mobile-store';

type Bytes = Uint8Array<ArrayBuffer>;
type Invitation = { relay: string; room: string; secret: string; publicKey: string; expiresAt: number; v: 1 };
type PacketReply<T> = { status: number; data?: T; detail?: string };

const text = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const token = /^[A-Za-z0-9_-]{43}$/;
const publicPoint = /^[A-Za-z0-9_-]{87}$/;
const MAX_REQUEST = 512 * 1024;
const MAX_ANSWER = 8 * 1024 * 1024;

export class RelayUnavailable extends Error {
  constructor() { super('暂未连上电脑。请保持电脑与中继在线，手机待发记录仍在本机。'); }
}

function encode(raw: Bytes) {
  return btoa(String.fromCharCode(...raw)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function decode(code: string): Bytes {
  if (!/^[A-Za-z0-9_-]+$/.test(code)) throw new Error('配对链接无效。');
  const value = Uint8Array.from(atob(code.replaceAll('-', '+').replaceAll('_', '/')), character => character.charCodeAt(0));
  if (encode(value) !== code) throw new Error('配对链接无效。');
  return value;
}

function context(value: string) {
  return text.encode('zhixue/mobile/v1/' + value);
}

function secret(raw: BufferSource, usages: KeyUsage[] = ['deriveKey']) {
  return crypto.subtle.importKey('raw', raw, 'HKDF', false, usages);
}

export function takeInvitation() {
  const url = new URL(window.location.href);
  const position = url.hash.indexOf('?');
  if (position < 0) return undefined;
  const params = new URLSearchParams(url.hash.slice(position + 1));
  const invitation = params.get('pair');
  if (invitation === null) return undefined;
  params.delete('pair');
  url.hash = url.hash.slice(0, position) + (params.size ? '?' + params : '');
  history.replaceState(history.state, '', url);
  return invitation;
}

export async function preparePair(encoded: string, name: string): Promise<MobilePair> {
  let invite: Invitation;
  let relay: URL;
  try {
    if (encoded.length > 4096) throw new Error();
    invite = JSON.parse(decoder.decode(decode(encoded))) as Invitation;
    relay = new URL(invite.relay);
  } catch {
    throw new Error('配对链接无效。请从电脑重新生成。');
  }
  if (invite.v !== 1 || relay.origin !== invite.relay || relay.origin === location.origin
    || (relay.protocol !== 'https:' && !(relay.protocol === 'http:' && relay.hostname === '127.0.0.1'))
    || !token.test(invite.room) || !token.test(invite.secret)
    || !publicPoint.test(invite.publicKey) || !Number.isSafeInteger(invite.expiresAt)) {
    throw new Error('配对链接无效。请从电脑重新生成。');
  }
  const desktop = await crypto.subtle.importKey(
    'raw', decode(invite.publicKey), { name: 'ECDH', namedCurve: 'P-256' }, false, [],
  );
  const phone = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const point = encode(new Uint8Array(await crypto.subtle.exportKey('raw', phone.publicKey)));
  const shared = await secret(await crypto.subtle.deriveBits({ name: 'ECDH', public: desktop }, phone.privateKey, 256), ['deriveBits']);
  const salt = decode(invite.secret);
  const derive = (purpose: string, length: number) => crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info: context(invite.room + '/' + purpose) }, shared, length,
  );
  const [root, room, fingerprint] = await Promise.all([
    derive('pair-key', 256), derive('pair-room', 256), derive('pair-code', 32),
  ]);
  return {
    relay: relay.origin, room: encode(new Uint8Array(room)), key: await secret(root),
    invitation: {
      relay: relay.origin, room: invite.room, key: await secret(salt), point, name: name.trim() || '手机',
      expiresAt: invite.expiresAt,
      code: Array.from(new Uint8Array(fingerprint), byte => byte.toString(16).padStart(2, '0')).join('').toUpperCase(),
    },
  };
}

async function cipher(device: RelayDevice, ticket: string, direction: 'request' | 'answer') {
  return crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: decode(ticket), info: context(device.room + '/' + direction) },
    device.key, { name: 'AES-GCM', length: 256 }, false, [direction === 'request' ? 'encrypt' : 'decrypt'],
  );
}

async function requestPacket(device: RelayDevice, ticket: string, command: object) {
  const plain = text.encode(JSON.stringify(command));
  if (plain.length + 28 > MAX_REQUEST) throw new Error('本机记录超出单次同步大小。');
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await cipher(device, ticket, 'request');
  const encrypted = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce }, key, plain));
  const packet = new Uint8Array(nonce.length + encrypted.length);
  packet.set(nonce);
  packet.set(encrypted, nonce.length);
  return packet;
}

async function boundedBody(response: Response, limit: number): Promise<Bytes> {
  const stream = response.body?.getReader();
  if (!stream) throw new RelayUnavailable();
  try {
    const chunks: Uint8Array[] = [];
    let length = 0;
    for (;;) {
      const { done, value } = await stream.read();
      if (done) break;
      length += value.length;
      if (length > limit) throw new RelayUnavailable();
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    return bytes;
  } finally {
    await stream.cancel().catch(() => {});
  }
}

async function relayFetch(url: string, body?: Bytes) {
  try {
    return await fetch(url, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/octet-stream' } : undefined,
      body, credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
      signal: AbortSignal.timeout(body ? 12000 : 2500),
    });
  } catch {
    throw new RelayUnavailable();
  }
}

export async function relayRequest<T>(device: RelayDevice, command: object): Promise<T> {
  const url = device.relay + '/v1/' + device.room;
  for (let attempt = 0; attempt < 5; attempt++) {
    if (attempt) await new Promise(resolve => window.setTimeout(resolve, 180));
    const offer = await relayFetch(url + '/offer');
    if (offer.status === 404) continue;
    if (!offer.ok) throw new RelayUnavailable();
    let ticket: unknown;
    try {
      ticket = JSON.parse(decoder.decode(await boundedBody(offer, 1024))).ticket;
    } catch {
      throw new RelayUnavailable();
    }
    if (typeof ticket !== 'string' || !token.test(ticket)) throw new RelayUnavailable();
    const packet = await requestPacket(device, ticket, command);
    const answer = await relayFetch(url + '/exchange/' + ticket, packet);
    if (answer.status === 404 || answer.status === 409) continue;
    if (!answer.ok) throw new RelayUnavailable();
    let response: PacketReply<T>;
    try {
      const key = await cipher(device, ticket, 'answer');
      const bytes = await boundedBody(answer, MAX_ANSWER);
      const plaintext = await crypto.subtle.decrypt({
        name: 'AES-GCM', iv: bytes.subarray(0, 12), additionalData: await crypto.subtle.digest('SHA-256', packet),
      }, key, bytes.subarray(12));
      response = JSON.parse(decoder.decode(plaintext)) as PacketReply<T>;
      if (!Number.isInteger(response.status)) throw new Error('Invalid response');
    } catch {
      throw new RelayUnavailable();
    }
    if (response.status !== 200) throw new ApiError(response.detail ?? '电脑暂时无法处理同步。', response.status, {});
    return response.data as T;
  }
  throw new RelayUnavailable();
}

export async function advancePair(pair: MobilePair) {
  const invite = pair.invitation;
  if (!invite) return true;
  try {
    await relayRequest(pair, { op: 'ping' });
  } catch (error) {
    if (!(error instanceof RelayUnavailable)) throw error;
    try {
      await relayRequest(invite, { op: 'pair', publicKey: invite.point, name: invite.name });
    } catch (failure) {
      if (failure instanceof RelayUnavailable && Date.now() > invite.expiresAt * 1000) {
        throw new Error('配对邀请已过期，请从电脑生成新链接。');
      }
      throw failure;
    }
    return false;
  }
  const paired = { relay: pair.relay, room: pair.room, key: pair.key };
  return savePair(paired, pair.room);
}
