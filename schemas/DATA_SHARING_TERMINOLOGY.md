# Data-Sharing Terminology

This glossary collects the data-sharing terminology used in the XFed project and terminology appearing in **Contract 1**, **Contract 2**, and **Contract 3**. Definitions are taken from primary standards or directly from the contracts where the term is contract-specific.

> **Note:** "Working definition" means the source supports the concept but does not define that exact phrase as a formal term.

| Term | Concise definition | Present in / relevance | Source of definition | Status |
|---|---|---|---|---|
| **Data** | Any digital representation of acts, facts or information, including compilations and sound, visual or audiovisual recordings. | Contract 1 | [EU Data Act, Art. 2(1)][DA] | Standard definition |
| **Metadata** | A structured description of the contents or use of data that facilitates discovery or use of the data. | Contract 1; useful for XFed policy metadata | [EU Data Act, Art. 2(2)][DA] | Standard definition |
| **Personal Data** | Information relating to an identified or identifiable natural person. | Contracts 1 and 3 | [GDPR, Art. 4(1)][GDPR] | Standard definition |
| **Non-personal Data** | Data other than personal data. | Contract 1 | [EU Data Act, Art. 2(4)][DA] | Standard definition |
| **User** | A person or organisation that owns a connected product, has temporary contractual rights to use it, or receives a related service. | Contract 1 | [EU Data Act, Art. 2(12)][DA] | Standard definition |
| **Data Holder** | A person or organisation that has the legal right or obligation to use and make data available under the Data Act or other applicable law. | Contract 1 | [EU Data Act, Art. 2(13)][DA] | Standard definition |
| **Data Recipient** | A business/professional party, other than the user, to whom a Data Holder makes data available, including a third party receiving it at the user's request. | Contract 1 | [EU Data Act, Art. 2(14)][DA] | Standard definition |
| **Data Sharer** | An enterprise holding data, having the right to make it available voluntarily, and controlling the data or means of access sufficiently to fulfil the sharing obligations. | Contract 1, Annex V | [Contract 1, Annex V §1][C1] | Contract definition |
| **Data Consumer** | A consumer of a dataset under a data contract; ODCS describes a data contract as an agreement between a data producer and consumers and defines access roles for consumers. | XFed terminology; not a defined term in Contracts 1-3 | [Bitol Open Data Contract Standard][ODCS]; [ODCS Roles][ODCS-ROLES] | Standards terminology, not a legal definition |
| **Data Access** | The ability or permission to obtain data from an asset or dataset under defined conditions. | Contracts 1-3; XFed policy rules | [W3C ODRL `read` action][ODRL-VOCAB] and EU Data Act Arts. 3-5 | Working definition |
| **Data Sharing / Making Data Available** | Providing another authorised party with access to data or otherwise making the data available under agreed or legally required conditions. | Contracts 1-3 | [EU Data Act, Arts. 1, 4 and 5][DA]; [Contract 1][C1] | Working definition based on statutory usage |
| **Data Copying / Reproduction** | Making duplicate copies of the data or other protected asset in material form. | Contract 2 §34.7; XFed policy actions | [W3C ODRL `reproduce` action][ODRL-VOCAB] | Standard policy-vocabulary definition |
| **Data Processing** | Any operation or set of operations performed on data, including collection, storage, retrieval, use, disclosure, combination, restriction, erasure or destruction. | Contracts 1 and 3; core XFed concept | [EU Data Act, Art. 2(7)][DA] | Standard definition |
| **Data Deletion** | Permanently removing all copies of an asset after it has been used; conditions can specify when deletion must occur. | Contracts 1 and 2; XFed duty/action | [W3C ODRL `delete` action][ODRL-VOCAB] | Standard policy-vocabulary definition |
| **Purpose** | A defined purpose for exercising an action permitted or governed by a policy rule. | Contracts 1-3; XFed constraint | [W3C ODRL `purpose` left operand][ODRL-VOCAB] | Standard policy-vocabulary definition |
| **Recipient (policy constraint)** | The party receiving the result or outcome of exercising a policy action. | Data-sharing restrictions in XFed | [W3C ODRL `recipient` left operand][ODRL-VOCAB] | Standard policy-vocabulary definition |
| **Recipient (personal-data context)** | A person, public authority, agency or other body to which personal data is disclosed, whether a third party or not, subject to the GDPR's public-authority exception. | Contracts involving personal data | [GDPR, Art. 4(9)][GDPR] | Standard definition |
| **Location / Spatial Constraint** | A geographic area that limits where an action governed by a policy may be exercised. | Contracts 1-3 where location restrictions apply; XFed constraint | [W3C ODRL `spatial` left operand][ODRL-VOCAB] | Standard policy-vocabulary definition |
| **Time Constraint** | A condition limiting when or for how long a policy action may be exercised. ODRL provides `dateTime` and `elapsedTime` operands for this purpose. | XFed policy constraints; contractual time limits | [W3C ODRL `dateTime` and `elapsedTime`][ODRL-VOCAB] | Standard policy-vocabulary concept |
| **Retention** | The period for which data is kept before deletion, return or another required disposal action. For personal data, it should be kept identifiable no longer than necessary for the processing purpose. | Contracts 1 and 2 | [GDPR, Art. 5(1)(e) - storage limitation][GDPR] | Working definition; GDPR principle for personal data |
| **Product Data** | Data generated by use of a connected product that the manufacturer designed to be retrievable by a user, Data Holder or third party. | Contract 1 | [EU Data Act, Art. 2(15)][DA] | Standard definition |
| **Related Service Data** | Data representing digitised user actions or events related to a connected product during provision of a related service. | Contract 1 | [EU Data Act, Art. 2(16)][DA] | Standard definition |
| **Readily Available Data** | Product Data and Related Service Data that a Data Holder lawfully obtains, or can lawfully obtain, without disproportionate effort beyond a simple operation. | Contract 1 | [EU Data Act, Art. 2(17)][DA] | Standard definition |
| **Trade Secret** | Information that is secret, has commercial value because it is secret, and has been subject to reasonable steps to keep it secret. | Contract 1; also relevant to proprietary data restrictions | [Directive (EU) 2016/943, Art. 2(1)][TSD] | Standard legal definition |
| **Confidential Information** | Business or technical information of the Company/Company Group disclosed to or discovered by the Contractor in connection with the agreement or work. In Contract 3, it means Background or other information disclosed by one Party to another under the collaboration agreement. | Contracts 2 and 3 | [Contract 2, Art. 30.1][C2]; [Contract 3, §1 Definitions][C3] | Contract-specific definitions |
| **Proprietary Information** | Information relating to Company operations, facilities, processes, plans, know-how, designs, trade secrets, software, wells, reservoirs and related business or technical matters, as further specified in Contract 2. | Contract 2 | [Contract 2, Art. 30.1][C2] | Contract definition |
| **Contractor Data** | Data gathered by the Contractor while performing work that is not required to be provided to the Company as a deliverable, including equipment-performance and raw sensor measurements, excluding Proprietary Data. | Contract 2 | [Contract 2, Art. 30.1][C2] | Contract definition |
| **Accessing Party** | A member of the Contractor Group to whom the Company grants access to designated portions of its systems and networks. | Contract 2 | [Contract 2, Art. 34.3][C2] | Contract-defined role |
| **Background** | Material contributions, intellectual-property rights, know-how and information that a Party brings to the Project. | Contract 3 | [Contract 3, §1 Definitions][C3] | Contract definition |
| **Project Results** | All results produced or achieved through work carried out under the Project, including intellectual-property rights, whether or not legally protected. | Contract 3 | [Contract 3, §1 Definitions][C3] | Contract definition |
| **Commercial Utilisation** | Direct or indirect use of Project Results in developing or marketing products, services or processes, or transferring/licensing use of Project Results to third parties. | Contract 3 | [Contract 3, §1 Definitions][C3] | Contract definition |
| **Fair and Reasonable Conditions** | Conditions for ownership or access that reflect the circumstances of the request, including value, contributions, scope, duration and characteristics of the planned exploitation. | Contract 3 | [Contract 3, §1 Definitions][C3] | Contract definition |
| **Access Rights** | Rights granted to another Party to use relevant Background or Project Results where needed for project work or commercial utilisation, subject to the conditions in the agreement. | Contract 3 §§10.4-10.5 and 11.2 | [Contract 3, §§10.4-10.5 and 11.2][C3] | Working definition from contract clauses |
| **Permission** | The ability to exercise an action over an asset. | XFed policy schema | [W3C ODRL Information Model 2.2][ODRL-MODEL] | Standard definition |
| **Prohibition** | The inability to exercise an action over an asset. | XFed policy schema | [W3C ODRL Information Model 2.2][ODRL-MODEL] | Standard definition |
| **Duty / Obligation** | The obligation to exercise an agreed action. | XFed policy schema | [W3C ODRL Information Model 2.2][ODRL-MODEL] | Standard definition |
| **Constraint** | A Boolean/logical expression that refines an action, party/asset collection, or conditions applicable to a policy rule. | XFed policy schema | [W3C ODRL Information Model 2.2][ODRL-MODEL] | Standard definition |

## Sources

### Standards and legislation

- **[DA] EU Data Act** - Regulation (EU) 2023/2854, especially Article 2 (Definitions) and Articles 3-5 (data access and making data available):  
  https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023R2854

- **[GDPR] General Data Protection Regulation** - Regulation (EU) 2016/679, especially Article 4 (Definitions) and Article 5(1)(e) (storage limitation):  
  https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679

- **[TSD] EU Trade Secrets Directive** - Directive (EU) 2016/943, Article 2(1):  
  https://eur-lex.europa.eu/eli/dir/2016/943/oj/eng

- **[ODRL-MODEL] W3C ODRL Information Model 2.2** - definitions of Permission, Prohibition, Duty and Constraint:  
  https://www.w3.org/TR/odrl-model/

- **[ODRL-VOCAB] W3C ODRL Vocabulary & Expression 2.2** - `read`, `reproduce`, `delete`, `purpose`, `recipient`, `spatial`, `dateTime`, `elapsedTime`, and related policy vocabulary:  
  https://www.w3.org/TR/odrl-vocab/

- **[ODCS] Bitol Open Data Contract Standard (ODCS)** - describes a data contract as an agreement between a data producer and consumers:  
  https://github.com/bitol-io/open-data-contract-standard

- **[ODCS-ROLES] Bitol ODCS Roles** - roles providing consumer/user access to a dataset and the type of access provided:  
  https://github.com/bitol-io/open-data-contract-standard/blob/main/docs/roles.md

### Project contract sources

These files are included in the XFed schema/source package and are the authoritative sources for contract-specific terminology.

- **[C1] [Contract 1](./Contract%201.PDF)** - European Commission Data Act Model Contractual Terms / Standard Contractual Clauses source used in the project. Relevant locations include Annex V §1 for **Data Sharer** and the contract sections adopting EU Data Act terminology.
- **[C2] [Contract 2](./Contract%202.pdf)** - Relevant locations: Article 30.1 for **Confidential Information**, **Contractor Data**, and **Proprietary Information**; Article 34.3 for **Accessing Party**; Article 34.7 for copying/access restrictions.
- **[C3] [Contract 3](./Contract%203.pdf)** - Relevant locations: §1 Definitions for **Background**, **Confidential Information**, **Commercial Utilisation**, **Fair and Reasonable Conditions**, and **Project Results**; §§10.4-10.5 and 11.2 for **Access Rights**.

## Terminology note for XFed

For XFed policy generation, the most useful separation is:

- **Contract/data-sharing roles:** Data Holder, User, Data Recipient, Data Sharer, Accessing Party.
- **Data objects:** Data, Product Data, Related Service Data, Readily Available Data, Metadata, Contractor Data, Background, Project Results.
- **Permitted/restricted operations:** access/read, processing/use, reproduction/copying, deletion, sharing/making available.
- **Policy restrictions:** purpose, recipient, location/spatial restrictions, time constraints, retention.
- **ODRL rule structure:** Permission, Prohibition, Duty, Constraint.

