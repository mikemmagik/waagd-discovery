import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const [siteModule, file] = process.argv.slice(2);
const { validatePublicationIntent, contentHashForPublicationIntent } = await import(pathToFileURL(siteModule).href);
const intent = JSON.parse(await readFile(file, 'utf8'));
const result = validatePublicationIntent(intent);
if (!result.valid || contentHashForPublicationIntent(intent) !== intent.content_hash) {
  console.error(JSON.stringify(result)); process.exit(1);
}
console.log(JSON.stringify({status:'accepted', content_hash:intent.content_hash}));
