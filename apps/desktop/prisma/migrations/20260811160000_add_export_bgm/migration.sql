-- 导出成片支持叠加背景音乐：本地音频文件路径 + 相对音量 + 默认开关，
-- 跟 exportBurnSubtitles 同一个"设置页存默认值，导出请求体可以临时覆盖"的模式。
ALTER TABLE "Setting" ADD COLUMN "exportBgmPath" TEXT;
ALTER TABLE "Setting" ADD COLUMN "exportBgmVolume" REAL NOT NULL DEFAULT 0.2;
ALTER TABLE "Setting" ADD COLUMN "exportUseBgm" BOOLEAN NOT NULL DEFAULT false;
