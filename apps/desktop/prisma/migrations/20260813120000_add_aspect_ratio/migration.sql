-- AlterTable
ALTER TABLE "Project" ADD COLUMN "aspectRatio" TEXT NOT NULL DEFAULT '9:16';

-- AlterTable
ALTER TABLE "VideoGeneration" ADD COLUMN "ratio" TEXT NOT NULL DEFAULT '9:16';
