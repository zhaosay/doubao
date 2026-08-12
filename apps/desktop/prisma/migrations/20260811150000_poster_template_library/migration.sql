-- 海报"类型"从写死的 hospital/guide/custom 三选一，改成开放的模版库(PosterTemplate)，
-- 用户可以自己增删改类型，不用等改代码。同时给价格表/知识卡片这类需要精确文字内容的
-- 场景加 layoutMode='textBlocks' + bodyLines 排版能力(见 poster_composer.py)。

-- AlterTable: PosterTemplate 加 layoutMode，标记这个模版默认用哪种排版。
ALTER TABLE "PosterTemplate" ADD COLUMN "layoutMode" TEXT NOT NULL DEFAULT 'title';

-- RedefineTable: Poster.category/customPrompt 拆掉，改成 templateId(可选，指向
-- PosterTemplate) + templateLabel/promptText(生成当下从模版复制的快照) +
-- layoutMode + bodyLines。SQLite 不支持直接改列，走标准的"建新表 -> 搬数据 ->
-- 删旧表 -> 改名"流程。
-- 旧数据迁移策略：category='custom' 的，promptText 直接搬 customPrompt 过去；
-- category='hospital'/'guide' 的，promptText 用当时代码里写死的同一段提示语
-- (跟下面预置的模版文案一致，只是老海报不反向关联到新模版行，templateId 留空，
-- 靠 templateLabel 记一下"这张当初是按哪个类型生成的")。
PRAGMA foreign_keys=OFF;

CREATE TABLE "new_Poster" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT,
    "orientation" TEXT NOT NULL DEFAULT 'portrait',
    "templateId" TEXT,
    "templateLabel" TEXT,
    "promptText" TEXT,
    "layoutMode" TEXT NOT NULL DEFAULT 'title',
    "bodyLines" TEXT,
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
    CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT "Poster_templateId_fkey" FOREIGN KEY ("templateId") REFERENCES "PosterTemplate" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

INSERT INTO "new_Poster" ("id", "projectId", "orientation", "templateLabel", "promptText", "layoutMode", "styleMode", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt")
SELECT "id", "projectId", "orientation",
    CASE "category" WHEN 'hospital' THEN '医院海报' WHEN 'guide' THEN '地陪翻译海报' ELSE '自定义' END,
    CASE "category"
        WHEN 'hospital' THEN '医疗/医院宣传海报主视觉，专业、干净、值得信赖的医疗环境氛围，可以出现医护人员、现代化医疗设备、明亮的医院环境等元素，色调明亮清爽。'
        WHEN 'guide' THEN '海外旅游地陪/医美陪同翻译服务宣传海报主视觉，专业亲切的服务氛围，可以出现地陪/翻译人员微笑服务、旅游或医美机构场景等元素，色调温暖友好。'
        ELSE "customPrompt"
    END,
    'title',
    "styleMode", "title", "subtitle", "extraPrompt", "referenceImagePaths", "backgroundPath", "filePath", "status", "error", "providerId", "model", "createdAt"
FROM "Poster";

DROP TABLE "Poster";
ALTER TABLE "new_Poster" RENAME TO "Poster";

CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId");
CREATE INDEX "Poster_templateId_idx" ON "Poster"("templateId");

PRAGMA foreign_keys=ON;

-- 预置 5 条默认模版，覆盖用户最初提到的几种业务场景，冷启动就有得选，
-- 用户可以随时在模版库里改名/改提示词/删除/新增，不锁死这几个。
INSERT INTO "PosterTemplate" ("id", "label", "promptText", "layoutMode", "createdAt") VALUES
('poster-tpl-hospital', '医院海报', '医疗/医院宣传海报主视觉，专业、干净、值得信赖的医疗环境氛围，可以出现医护人员、现代化医疗设备、明亮的医院环境等元素，色调明亮清爽。', 'title', CURRENT_TIMESTAMP),
('poster-tpl-guide', '地陪翻译海报', '海外旅游地陪/医美陪同翻译服务宣传海报主视觉，专业亲切的服务氛围，可以出现地陪/翻译人员微笑服务、旅游或医美机构场景等元素，色调温暖友好。', 'title', CURRENT_TIMESTAMP),
('poster-tpl-health', '医美科普海报', '医美/医疗科普知识主视觉，专业权威、清晰易懂的视觉风格，画面简洁大方，适合承载科普类图文内容，色调清新明亮。', 'title', CURRENT_TIMESTAMP),
('poster-tpl-price', '价格表海报', '医美/医疗项目价格表海报主视觉，简洁大方的背景，画面留白干净适合叠加价格列表文字，避免复杂花纹干扰阅读，色调专业沉稳。', 'textBlocks', CURRENT_TIMESTAMP),
('poster-tpl-card', '知识卡片', '医美知识科普卡片主视觉，清爽简洁的卡片式背景，画面干净适合叠加多条知识点文字，色调柔和易读。', 'textBlocks', CURRENT_TIMESTAMP);
