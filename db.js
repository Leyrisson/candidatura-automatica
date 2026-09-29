"use strict";
// Banco SQLite do pipeline de Candidatura Automática.
// Arquivo único em /workflows/host/vagas.db (persistido no host via volume ./workflows).
const { DatabaseSync } = require("node:sqlite");
const path = require("path");

const DB_PATH = process.env.VAGAS_DB || path.join(__dirname, "..", "vagas.db");

let _db = null;

function db() {
  if (_db) return _db;
  _db = new DatabaseSync(DB_PATH);
  _db.exec("PRAGMA journal_mode=WAL;");
  _db.exec(`
    CREATE TABLE IF NOT EXISTS vagas (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id    TEXT UNIQUE,
      subject       TEXT,
      from_addr     TEXT,
      link          TEXT,
      snippet       TEXT,
      local         TEXT,          -- 'SP' | 'Sorocaba' | 'Remoto' | ''
      regime        TEXT,          -- 'CLT' | ''
      areas         TEXT,          -- JSON array: ['suporte','redes',...]
      exigencia     TEXT,          -- 'nenhuma' | 'superior' | 'certificacao' | 'ambas'
      aprovado      INTEGER DEFAULT 0,  -- 1 = elegível
      motivo        TEXT,          -- por que aprovou/recusou
      etapa         TEXT DEFAULT 'detectada',  -- detectada|analisada|candidatura|questionario|enviada|monitorada|reprovada
      origem        TEXT,          -- url da vaga lida
      criado_em     TEXT DEFAULT (datetime('now')),
      atualizado_em TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS respostas (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      pergunta_norm TEXT UNIQUE,   -- pergunta normalizada (minúsculo, sem pontuação)
      pergunta      TEXT,          -- pergunta original
      resposta      TEXT,          -- sua resposta no Telegram
      area          TEXT,          -- contexto opcional (telefone, cidade, salario...)
      fonte          TEXT,          -- link/origem da primeira vez que respondeu
      criado_em     TEXT DEFAULT (datetime('now')),
      atualizado_em TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS etapas (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      vaga_id    INTEGER REFERENCES vagas(id),
      etapa      TEXT,
      detalhe    TEXT,
      criado_em  TEXT DEFAULT (datetime('now'))
    );
  `);
  return _db;
}

function normalizaPergunta(p) {
  return (p || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Insere/atualiza uma vaga detectada (idempotente por message_id).
function upsertVaga(v) {
  const d = db();
  const st = d.prepare(`INSERT INTO vagas (message_id, subject, from_addr, link, snippet, criado_em, atualizado_em)
    VALUES (?,?,?,?,?,datetime('now'),datetime('now'))
    ON CONFLICT(message_id) DO UPDATE SET subject=excluded.subject, snippet=excluded.snippet, atualizado_em=datetime('now')`);
  st.run(v.messageId, v.subject, v.from, v.link, v.snippet);
  const row = d.prepare(`SELECT id FROM vagas WHERE message_id=?`).get(v.messageId);
  return row.id;
}

// Grava a análise de elegibilidade.
function gravaAnalise(messageId, a) {
  const d = db();
  d.prepare(`UPDATE vagas SET local=?, regime=?, areas=?, exigencia=?, aprovado=?, motivo=?, etapa=?, origem=?, atualizado_em=datetime('now')
    WHERE message_id=?`).run(
      a.local || "", a.regime || "", JSON.stringify(a.areas || []), a.exigencia || "nenhuma",
      a.aprovado ? 1 : 0, a.motivo || "", a.etapa || "analisada", a.origem || "", messageId);
}

function achaResposta(pergunta) {
  const d = db();
  const n = normalizaPergunta(pergunta);
  return d.prepare(`SELECT * FROM respostas WHERE pergunta_norm=?`).get(n);
}

function gravaResposta(pergunta, resposta, area, fonte) {
  const d = db();
  const n = normalizaPergunta(pergunta);
  d.prepare(`INSERT INTO respostas (pergunta_norm, pergunta, resposta, area, fonte, criado_em)
    VALUES (?,?,?,?,?,datetime('now'))
    ON CONFLICT(pergunta_norm) DO UPDATE SET resposta=excluded.resposta, atualizado_em=datetime('now')`)
    .run(n, pergunta, resposta, area || "", fonte || "");
}

function proximaEtapa(vagaId, etapa, detalhe) {
  const d = db();
  d.prepare(`UPDATE vagas SET etapa=?, atualizado_em=datetime('now') WHERE id=?`).run(etapa, vagaId);
  d.prepare(`INSERT INTO etapas (vaga_id, etapa, detalhe, criado_em) VALUES (?,?,?,datetime('now'))`)
    .run(vagaId, etapa, detalhe || "");
}

function listaVagas() {
  return db().prepare(`SELECT * FROM vagas ORDER BY id DESC`).all();
}

module.exports = { db, normalizaPergunta, upsertVaga, gravaAnalise, achaResposta, gravaResposta, proximaEtapa, listaVagas, DB_PATH };
