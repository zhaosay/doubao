-- RedefineTable: Poster.projectId 从必填外键改成可选(ON DELETE SET NULL)，
-- 新增 styleMode 字段——海报独立成一级功能，不再要求必须挂在某个 Project 下。
-- SQLite 不支持直接 ALTER COLUMN 去掉 NOT NULL / 改 FK 行为，走标准的
-- "建新表 -> 搬数据 -> 删旧表 -> 改名" 流程。
PRAGMA foreign_keys=OFF;

CREATE TABLE "new_Poster" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT,
    "presetId" TEXT NOT NULL,
    "styleMode" TEXT NOT NULL DEFAULT 'comic',
    "title" TEXT NOT NULL,
    "subtitle" TEXT,
    "extraPrompt" TEXT,
    "referenceImagePaths" TEXT,
    "backgroundPath" TEXT,
    "filePath" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "providerId" TEXT,
    "model" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

INSERT INTO "new_Poster" ("id", "projectId", "presetId", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt")
SELECT "id", "projectId", "presetId", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt" FROM "Poster";

DROP TABLE "Poster";
ALTER TABLE "new_Poster" RENAME TO "Poster";

CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId");

PRAGMA foreign_keys=ON;
