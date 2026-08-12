-- RedefineTable: Poster.presetId 拆成两个独立维度 orientation(竖版/横版) + category
-- (医院海报/地陪翻译/自定义)，新增 customPrompt 存自定义类型下用户写的提示词。
-- SQLite 不支持直接改列，走标准的"建新表 -> 搬数据 -> 删旧表 -> 改名"流程。
-- 旧数据迁移策略：presetId='poster_landscape' -> orientation='landscape'，其余(
-- poster_drama/poster_character) -> orientation='portrait'；category 统一给一个
-- 合理默认值 'hospital'（老数据本来就没有类型这个概念，选哪个都是猜，选医院海报
-- 只是因为这是用户最主要的业务场景，用户可以自己在列表里改）。
PRAGMA foreign_keys=OFF;

CREATE TABLE "new_Poster" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT,
    "orientation" TEXT NOT NULL DEFAULT 'portrait',
    "category" TEXT NOT NULL DEFAULT 'hospital',
    "customPrompt" TEXT,
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

INSERT INTO "new_Poster" ("id", "projectId", "orientation", "category", "styleMode", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt")
SELECT "id", "projectId",
    CASE WHEN "presetId" = 'poster_landscape' THEN 'landscape' ELSE 'portrait' END,
    'hospital',
    "styleMode", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt"
FROM "Poster";

DROP TABLE "Poster";
ALTER TABLE "new_Poster" RENAME TO "Poster";

CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId");

PRAGMA foreign_keys=ON;

-- 新增自定义模版库：用户在"自定义"类型下写的提示词可以存成一条模版，下次直接选，
-- 不用每次重新打字。跟 Poster 完全独立，没有外键关联——模版被删掉不影响已经用过
-- 它生成的海报(Poster.customPrompt 是当次生成时复制的一份，不是引用)。
CREATE TABLE "PosterTemplate" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "label" TEXT NOT NULL,
    "promptText" TEXT NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
