-- AlterTable
ALTER TABLE "Asset" ADD COLUMN "model" TEXT;

-- AlterTable
ALTER TABLE "Character" ADD COLUMN "model" TEXT;
ALTER TABLE "Character" ADD COLUMN "providerId" TEXT;

-- AlterTable
ALTER TABLE "Scene" ADD COLUMN "model" TEXT;
ALTER TABLE "Scene" ADD COLUMN "providerId" TEXT;
