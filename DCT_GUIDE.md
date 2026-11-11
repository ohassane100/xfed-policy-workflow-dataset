# DCT Authoring guide

Open **DCT authoring** to build a data collaboration agreement. Tick a box to include its terms. Text boxes name the parties, resources, and purpose. Use the number and date fields to set deadlines and project dates.

## Parties and information

- **Agreement title** — the title printed on the PDF.
- **Provider** — the organisation that supplies the data.
- **Recipient** — the organisation that receives or uses the data. Use a different name from the Provider.
- **Information or dataset** — the name of the data covered by the agreement.
- **Permitted purpose** — what the Recipient is allowed to use the data for, such as joint research. The reading permission and purpose restriction use this value when selected.
- **Extended agreement with operational schedules** — add preset sections for topics such as delivery, access records, local copies, backups, and incident handling. Each schedule adds its own terms.

## Data-sharing terms

- **Dataset scope and contents** — optionally describe what the dataset contains in one line. For example, enter “Pseudonymised turbine measurements and maintenance logs.”
- **Permit reading for the stated purpose** — allow the Recipient to read the dataset for the permitted purpose.
- **Prohibit use for other purposes** — forbid the Recipient from using the dataset for a different purpose.
- **Prohibit sharing with third parties** — forbid the Recipient from sharing the dataset with third parties, subject to any selected exceptions.
- **Allow an exception for Provider's prior written consent** — add a written-consent exception to the third-party sharing ban. The Provider must give consent before sharing.
- **Require confidentiality** — require the Recipient to keep the dataset confidential.
- **Require secure storage** — require the Recipient to store the dataset securely.
- **Require deletion after termination** — require the Recipient to delete the dataset when the agreement ends, within the deadline below.
- **Deletion deadline in days** — the number of days after termination allowed for deletion. This deadline applies when the deletion box is ticked.
- **Require incident notification to Provider** — require the Recipient to notify the Provider about unauthorised access to, or accidental loss of, the dataset.
- **Incident deadline in hours** — the number of hours the Recipient has to notify the Provider after becoming aware of the incident.

## Additional safeguards and exceptions

- **Require AES-256 encryption** — require the Recipient to encrypt the dataset using AES-256.
- **Restrict storage countries** — prohibit storage outside the permitted countries.
- **Permitted storage countries** — enter one to four different countries separated by commas, such as `Norway, Sweden`. The form supports Norway, Sweden, Denmark, Finland, Iceland, Germany, France, and the Netherlands.
- **Except sharing that is required by law** — add a legal disclosure exception to the third-party sharing ban.
- **Except deletion while retention is required by law** — exempt data from the deletion duty while the law requires keeping it.

## Project resources, access and audit

- **Prohibit developing competing products with the data** — forbid the Recipient from using the dataset to develop competing products.
- **Provider-controlled platform access with continuing security requirements** — let the Provider decide whether to grant direct electronic access. Access depends on the Recipient continuing to meet the security requirements of multi-factor authentication and an approved project account.
- **Platform name** — the name of the system covered by the platform access clauses.
- **Complete processing records and access on request** — require the Recipient to keep complete records of processing under the agreement and make them available to the Provider on request.
- **Processing records** — the name of those records in the agreement.

## Background materials and work plan

- **Time-limited, no-charge access for necessary project work** — allow the Recipient to read the background materials for free during the project, as needed for its own work under the work plan. The terms also limit use to the permitted purpose or the Provider's prior written consent.
- **Background materials** — the name of the existing materials made available for the project.
- **Work plan** — the name of the plan that sets the scope of project work.
- **Project start** — the first day of the project period. The background materials access window includes this date.
- **Project end** — the last day of the project period. The background materials access window includes this date. Enter the start date or a later date.

## Publication and objections

- **Publication notice, objection window and bounded delay requests** — require the Recipient to notify the Provider before publishing results. Give the Provider a limited window to request a delay. The publication terms also require either confirmation of no objection or resolution of objections, as applicable, and no disclosure of confidential information.
- **Research results** — the name of the results covered by publication or attribution terms.
- **Objection window in days** — the number of days after the publication notice in which the Provider can request a delay. This also sets the waiting period for publication with confirmed no objection.
- **Maximum publication delay in days** — the longest delay the Provider can request. The template also uses this period when setting the earliest publication date after an objection.

## Delivery, responsible use and close-out

- **Limit dataset access to authorised project researchers** — forbid the Recipient from giving access to anyone outside the authorised researcher group.
- **Authorised researcher group** — the name of the eligible Recipient personnel. The agreement limits the group to registered project personnel bound by its confidentiality requirements.
- **Prohibit re-identification of individuals** — forbid using the dataset to identify the people it describes.
- **Require Provider attribution in publications** — require the Recipient to credit the Provider when publishing the research results.
- **Require notification of discovered dataset errors** — require the Recipient to report material inaccuracies in the dataset or its documentation to the Provider.
- **Error notification deadline in hours** — the number of hours the Recipient has to report an error after discovering it.
- **Require a deletion certificate after data deletion** — require the Recipient to send a certificate to the Provider once the dataset has been deleted.
- **Deletion certificate** — the name of the signed record of deleted copies and copies kept under a legal retention requirement.
- **Certificate deadline in days** — the number of days the Recipient has to send the certificate after deleting the dataset.
- **Require Provider delivery of data and documentation** — require the Provider to send both the dataset and its documentation to the Recipient.
- **Dataset documentation** — the name of the data dictionary and processing notes. The delivery and error-reporting terms use it when selected.
- **Delivery deadline in days after signature** — the number of days the Provider has to deliver the dataset and documentation after the agreement is signed.

## Start or save an agreement

- **Saved DCT contracts** — choose **Create a new agreement** for a blank form, **Default DCT example agreement** to load the basic example, or a saved agreement to edit its terms. Saving edited terms creates a new revision.
- **Research collaboration preset** — fill in an example with an example preset contract.
- **30-page evaluation preset** — fill in with the 30-page evaluation preset contract. This preset is for testing the extraction model and evaluation scoring.
- **Update preview** — see the contract text and expected rule count for the current choices.
- **Create contract PDF** — save the agreement and its evaluation answer key. The extraction model reads the PDF. Evaluation uses the saved answer key to score the outputs.

# Use the reusable clause library

1. Run the agreement through **Policy creation**. Eligible extractions appear as cases with the clause text, supporting context, and generated rules.
2. Open **Step 3 · Policy review** in the pipeline, or expand **Reusable clause library** in **Contracts**.
3. Expand a case. Read its rule alongside the clause and context. Use **Open source PDF** to check the surrounding contract.
4. Click **Approve this interpretation for exact reuse** when the rule captures the clause correctly. The library labels it **Established / approved**. Use **Revoke reuse approval** if the approval needs to be withdrawn.
5. On a later run, keep **Reuse approved clauses and retrieve related examples** ticked in **Pipeline**. After policy creation, check the reuse counts above the **Generation report**.

- **Exact match with approval** — when the clause, supporting context, model, and extraction configuration match, XFed reuses the approved interpretation and links it to the current PDF.
- **Similar or changed clause** — XFed generates a fresh rule and can use an approved related example as guidance. Changing a deletion deadline from 14 to 28 days calls for a fresh rule even if the rest of the sentence stays the same.
- **New clause** — XFed generates a rule from the current agreement. You can review the new interpretation for future reuse.
- **Evaluation** runs the extraction with clause reuse and retrieval disabled, so the scores show how the pipeline interpreted the PDF on that run.

