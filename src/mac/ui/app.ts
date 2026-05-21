// Maintained dashboard entry point. The dashboard implementation lives in
// modules/ — see modules/bootstrap.ts for the boot sequence and modules/
// README expectations. The browser ships the type-stripped version of this
// tree as src/mac/ui/app.js so mac does not require Node.js/npm to serve or
// install the UI.
import { bootstrap } from "./modules/bootstrap.js";

bootstrap();
