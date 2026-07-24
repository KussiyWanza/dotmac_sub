from app.models.admin_alert import AdminAlert, AdminNotification  # noqa: F401
from app.models.admin_whats_new import AdminWhatsNewItem  # noqa: F401
from app.models.ai_insight import (  # noqa: F401
    AIInsight,
    AIInsightStatus,
    InsightDomain,
    InsightSeverity,
)
from app.models.ai_intake import AiIntakeConfig  # noqa: F401
from app.models.analytics import (  # noqa: F401
    KPIAggregate,
    KPIConfig,
)
from app.models.audit import AuditActorType, AuditEvent  # noqa: F401
from app.models.auth import (  # noqa: F401
    ApiKey,
    MFAMethod,
    MFARecoveryCode,
    Session,
    UserCredential,
)
from app.models.bandwidth import BandwidthSample, QueueMapping  # noqa: F401
from app.models.billing import (  # noqa: F401
    AccountAdjustment,
    AccountCreditApplicationPolicy,
    BankAccount,
    BankAccountType,
    BankReconciliationItem,
    BankReconciliationRun,
    BillingAccount,
    BillingAccountCreditAllocation,
    BillingAccountCreditAllocationItem,
    BillingAccountLedgerEntry,
    BillingRun,
    BillingRunSchedule,
    BillingRunStatus,
    ConsolidatedCreditConsumptionReconciliationEvidence,
    ConsolidatedPaymentReturnDocumentReconstructionEvidence,
    ConsolidatedPaymentReturnReconciliationEvidence,
    ConsolidatedPaymentSettlementReconciliationEvidence,
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceClosure,
    InvoiceClosureLedgerEvidence,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceLine,
    InvoicePdfExport,
    InvoicePdfExportStatus,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocationReconciliationException,
    PaymentMethod,
    PaymentMethodType,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
    TopupAllocationPolicy,
    TopupIntent,
    TopupIntentPurpose,
)
from app.models.branding import BrandProfile  # noqa: F401
from app.models.catalog import (  # noqa: F401
    AccessCredential,
    AccessType,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    CatalogOffer,
    ConfigBackupMethod,
    ConnectionType,
    ContractTerm,
    DiscountType,
    DunningAction,
    ExecutionMethod,
    HealthStatus,
    NasConfigBackup,
    NasConnectionRule,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    OfferAddOn,
    OfferPrice,
    OfferRadiusProfile,
    OfferStatus,
    OfferVersion,
    OfferVersionPrice,
    PlanCategory,
    PolicyDunningStep,
    PolicySet,
    PriceBasis,
    PriceType,
    PriceUnit,
    ProrationPolicy,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
    ProvisioningTemplate,
    RadiusAttribute,
    RadiusProfile,
    RefundPolicy,
    RegionZone,
    ServiceType,
    SlaProfile,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    SuspensionAction,
    UsageAllowance,
)
from app.models.collections import (  # noqa: F401
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
    FinancialAccessAction,
    FinancialAccessConsequence,
    FinancialAccessConsequenceEvidence,
    FinancialAccessEvidenceOperation,
    FinancialAccessOrigin,
)
from app.models.comms import (  # noqa: F401
    CustomerNotificationEvent,
    CustomerNotificationStatus,
    Survey,
)
from app.models.comms_campaign import (  # noqa: F401
    Campaign,
    CampaignChannel,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignSender,
    CampaignStatus,
    CampaignStep,
    CampaignType,
)
from app.models.communication_log import (  # noqa: F401
    CommunicationChannel,
    CommunicationDirection,
    CommunicationLog,
    CommunicationStatus,
)
from app.models.compensation_failure import (  # noqa: F401
    CompensationFailure,
    CompensationStatus,
)
from app.models.connectivity_backup import (  # noqa: F401
    ConnectivityStateBackup,
)
from app.models.connector import (  # noqa: F401
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorType,
)
from app.models.contracts import ContractSignature  # noqa: F401
from app.models.crm_sync_failure import (  # noqa: F401
    CrmSyncFailure,
    CrmSyncFailureStatus,
)
from app.models.cross_app_drift import (  # noqa: F401
    CrossAppDriftFinding,
    CrossAppDriftFindingEvent,
    CrossAppDriftRun,
    CrossAppDriftWaiver,
)
from app.models.customer_experience import (  # noqa: F401
    CustomerExperienceHandoff,
    CustomerExperienceHandoffEvent,
    CustomerExperienceHandoffStatus,
)
from app.models.customer_identity import CustomerIdentityIndex  # noqa: F401
from app.models.customer_tax_policy import CustomerTaxPolicy  # noqa: F401
from app.models.cutover import CutoverBalanceVariance  # noqa: F401
from app.models.device_token import DeviceToken  # noqa: F401
from app.models.dispatch import (  # noqa: F401
    AvailabilityBlock,
    DispatchQueueStatus,
    DispatchRule,
    Shift,
    Skill,
    TechnicianProfile,
    TechnicianSkill,
    WorkOrderAssignmentQueue,
)
from app.models.domain_settings import (  # noqa: F401
    DomainSetting,
    SettingDomain,
)
from app.models.enforcement_lock import (  # noqa: F401
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.erp_domain_sync import ErpDomainSyncCursor  # noqa: F401
from app.models.event_store import (  # noqa: F401
    EventHandlerAttempt,
    EventStatus,
    EventStore,
)
from app.models.external import (  # noqa: F401
    ExternalEntityType,
    ExternalReference,
)
from app.models.fiber_access_attachment import (  # noqa: F401
    FiberAccessAttachmentDecision,
    SplitterCascadeLink,
)
from app.models.fiber_change_request import (  # noqa: F401
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.fiber_physical import (  # noqa: F401
    FiberConnectorPort,
    FiberCoreSplice,
    FiberPatchCord,
    FiberPatchPanel,
    FiberPhysicalLinkDecision,
    FiberRack,
    FiberStrandTermination,
)
from app.models.fiber_support import (  # noqa: F401
    FiberSupportMount,
    FiberSupportMountDecision,
    FiberSupportStructure,
)
from app.models.fiber_topology_connectivity import (  # noqa: F401
    FiberTopologyConnectivityDecision,
    FiberTopologySegmentSourceLink,
    FiberTopologyTerminationResolution,
)
from app.models.fiber_topology_connectivity_review import (  # noqa: F401
    FiberTopologyConnectivityBatchReview,
    FiberTopologyConnectivityProposalBatch,
    FiberTopologyConnectivityRun,
)
from app.models.fiber_topology_field_observation import (  # noqa: F401
    FiberTopologyFieldObservation,
)
from app.models.fiber_topology_identity import (  # noqa: F401
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityBatchReview,
    FiberTopologyIdentityDecision,
    FiberTopologyIdentityExecutionRun,
    FiberTopologyIdentityProposalBatch,
)
from app.models.fiber_topology_staging import (  # noqa: F401
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.field_asset import (  # noqa: F401
    FieldAsset,
    FieldAssetCustody,
)
from app.models.field_attachment import FieldAttachment  # noqa: F401
from app.models.field_chat import FieldJobChatMessage  # noqa: F401
from app.models.field_erp_sync import (  # noqa: F401
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_expense import (  # noqa: F401
    FieldExpenseRequest,
    FieldExpenseRequestItem,
)
from app.models.field_fiber import FieldFiberTestResult  # noqa: F401
from app.models.field_job_event import FieldJobEvent  # noqa: F401
from app.models.field_location import (  # noqa: F401
    FieldTechLocationPing,
    FieldTechPresence,
)
from app.models.field_map import FieldMapAssetLocationProvenance  # noqa: F401
from app.models.field_material import (  # noqa: F401
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldMaterialRequestItem,
    FieldWorkOrderMaterial,
)
from app.models.field_movement import FieldWorkOrderMovement  # noqa: F401
from app.models.field_note import FieldWorkOrderNote  # noqa: F401
from app.models.field_vendor import (  # noqa: F401
    FieldVendor,
    FieldVendorDeviceToken,
    FieldVendorUser,
)
from app.models.field_worklog import FieldWorkLog  # noqa: F401
from app.models.forwarding_topology import (  # noqa: F401
    ForwardingControlObservation,
    ForwardingTopologyDecision,
    ForwardingTopologyDeclaration,
)
from app.models.fup import (  # noqa: F401
    FupAction,
    FupConsumptionPeriod,
    FupDataUnit,
    FupDirection,
    FupPolicy,
    FupRule,
)
from app.models.fup_state import FupActionStatus, FupState  # noqa: F401
from app.models.gis import (  # noqa: F401
    GeoArea,
    GeoAreaType,
    GeoLayer,
    GeoLayerSource,
    GeoLayerType,
    GeoLocation,
    GeoLocationType,
    ServiceBuilding,
)
from app.models.idempotency import IdempotencyKey  # noqa: F401
from app.models.imports import (  # noqa: F401
    ImportRowStatus,
    ImportRun,
    ImportRunRow,
    ImportRunStatus,
    PaymentImportBatchReversal,
    PaymentImportBatchReversalItem,
)
from app.models.integration import (  # noqa: F401
    IntegrationJob,
    IntegrationJobType,
    IntegrationRecord,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTarget,
    IntegrationTargetType,
)
from app.models.integration_connector import (  # noqa: F401
    IntegrationConnector,
    IntegrationConnectorStatus,
    IntegrationConnectorType,
)
from app.models.integration_platform import (  # noqa: F401
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationCheckpoint,
    IntegrationConfigRevision,
    IntegrationDelivery,
    IntegrationEventSubscription,
    IntegrationInbox,
    IntegrationInstallation,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.models.legal import (  # noqa: F401
    LegalDocument,
    LegalDocumentType,
)
from app.models.lifecycle import (  # noqa: F401
    LifecycleEventType,
    SubscriptionLifecycleEvent,
)
from app.models.location_capture_prompt import (  # noqa: F401
    LocationCapturePromptState,
)
from app.models.mrr_snapshot import MrrSnapshot  # noqa: F401
from app.models.network import (  # noqa: F401
    AuthorizationPreset,
    BulkProvisioningItem,
    BulkProvisioningItemStatus,
    BulkProvisioningRun,
    BulkProvisioningRunStatus,
    ConfigMethod,
    CPEDevice,
    DeviceStatus,
    DeviceType,
    FdhCabinet,
    FiberAccessPoint,
    FiberEndpointType,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    GponChannel,
    HardwareUnitStatus,
    IPAssignment,
    IpBlock,
    IpPool,
    IpProtocol,
    IPv4Address,
    IPv6Address,
    IPVersion,
    MgmtIpMode,
    NetworkZone,
    ODNEndpointType,
    OltCard,
    OltCardPort,
    OltConfigBackup,
    OltConfigBackupType,
    OLTDevice,
    OltLineProfile,
    OltLineProfileGemMapping,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltPortType,
    OltPowerUnit,
    OltServicePort,
    OltServiceProfile,
    OltSfpModule,
    OltShelf,
    OntAssignment,
    OntProfileType,
    OntProfileWanService,
    OntProvisioningEvent,
    OntProvisioningEventStatus,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntStatusSource,
    OntSyncStatus,
    OntUnit,
    OnuCapability,
    OnuMode,
    OnuOfflineReason,
    OnuOnlineStatus,
    OnuType,
    PonPort,
    PonPortSplitterLink,
    PonType,
    Port,
    PortStatus,
    PortType,
    PortVlan,
    PppoePasswordMode,
    SignalThresholdOverride,
    SpeedProfile,
    SpeedProfileDirection,
    SpeedProfileType,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
    Tr069ParameterMap,
    VendorModelCapability,
    VendorSnmpConfig,
    Vlan,
    VlanMode,
    WanConnectionType,
    WanMode,
    WanServiceType,
)
from app.models.network_monitoring import (  # noqa: F401
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DnsThreatAction,
    DnsThreatEvent,
    DnsThreatSeverity,
    InterfaceStatus,
    MetricType,
    NetworkDevice,
    NetworkDeviceBandwidthGraph,
    NetworkDeviceBandwidthGraphSource,
    NetworkDeviceSnmpOid,
    NetworkTopologyLink,
    NetworkWeathermapView,
    PopSite,
    PopSiteContact,
    SpeedTestResult,
    SpeedTestSource,
    TopologyLinkAdminStatus,
    TopologyLinkMedium,
    TopologyLinkRole,
)
from app.models.network_monitoring import (  # noqa: F401
    DeviceStatus as MonitoringDeviceStatus,
)
from app.models.network_monitoring import (  # noqa: F401
    DeviceType as MonitoringDeviceType,
)
from app.models.network_operation import (  # noqa: F401
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.notification import (  # noqa: F401
    AlertNotificationLog,
    AlertNotificationPolicy,
    AlertNotificationPolicyStep,
    CommunicationIntentRecord,
    CommunicationSuppression,
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    NotificationTemplate,
    OnCallRotation,
    OnCallRotationMember,
)
from app.models.oauth_token import OAuthToken  # noqa: F401
from app.models.offer_availability import (  # noqa: F401
    OfferBillingModeAvailability,
    OfferCategoryAvailability,
    OfferLocationAvailability,
    OfferResellerAvailability,
)
from app.models.ont_assignment_constraint_authorization import (  # noqa: F401
    OntAssignmentConstraintAuthorizationRequest,
    OntAssignmentConstraintAuthorizationReview,
)
from app.models.ont_assignment_cutover import (  # noqa: F401
    OntAssignmentCutoverBatchReview,
    OntAssignmentCutoverProposalBatch,
    OntAssignmentCutoverVerificationAttestation,
)
from app.models.ont_assignment_identity import (  # noqa: F401
    OntAssignmentIdentityDecision,
)
from app.models.ont_autofind import OltAutofindCandidate  # noqa: F401
from app.models.ont_observation import OntObservation  # noqa: F401
from app.models.ont_topology_observation import (  # noqa: F401
    OntTopologyObservationEvidence,
)
from app.models.operational_escalation import (  # noqa: F401
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalRoomProvider,
    OperationalWatcher,
    OperationalWatcherRole,
)
from app.models.organization import (  # noqa: F401
    Organization,
    OrganizationAccountStatus,
    OrganizationAccountType,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.party import (  # noqa: F401
    PartnerRoleKey,
    Party,
    PartyContactConsentStatus,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyDataClassification,
    PartyExternalReference,
    PartyIdentityBackfillReceipt,
    PartyIdentityStatus,
    PartyMembership,
    PartyMembershipStatus,
    PartyMembershipType,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
    PartyRole,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
    SubscriberContactPointProjection,
    SubscriberContactRelationshipProjection,
)
from app.models.payment_arrangement import (  # noqa: F401
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentArrangementInstallment,
    PaymentFrequency,
)
from app.models.payment_proof import (  # noqa: F401
    PaymentProof,
    PaymentProofStatus,
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
    WithholdingTaxTransitionImmutableError,
)
from app.models.portal_message import (  # noqa: F401
    PortalMessage,
    PortalMessageStatus,
    PortalMessageType,
)
from app.models.prepaid_coverage import (  # noqa: F401
    PrepaidCoverageReconciliationItem,
    PrepaidCoverageReconciliationRun,
)
from app.models.prepaid_enforcement import PrepaidEnforcementReadiness  # noqa: F401
from app.models.prepaid_funding import (  # noqa: F401
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.project import (  # noqa: F401
    Project,
    ProjectComment,
    ProjectPriority,
    ProjectStatus,
    ProjectTask,
    ProjectTaskAssignee,
    ProjectTaskComment,
    ProjectTaskDependency,
    ProjectTaskDependencyType,
    ProjectTaskPriority,
    ProjectTaskStatus,
    ProjectTemplate,
    ProjectTemplateTask,
    ProjectTemplateTaskDependency,
    ProjectType,
)
from app.models.provisioning import (  # noqa: F401
    AppointmentStatus,
    ProvisioningReadinessCheck,
    ProvisioningReadinessCheckKind,
    ProvisioningReadinessCheckResult,
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ProvisioningRun,
    ProvisioningRunStatus,
    ProvisioningStep,
    ProvisioningStepType,
    ProvisioningTask,
    ProvisioningVendor,
    ProvisioningWorkflow,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
    ServiceState,
    ServiceStateTransition,
    TaskStatus,
)
from app.models.qualification import (  # noqa: F401
    BuildoutMilestone,
    BuildoutMilestoneStatus,
    BuildoutProject,
    BuildoutProjectStatus,
    BuildoutRequest,
    BuildoutRequestStatus,
    BuildoutStatus,
    BuildoutUpdate,
    CoverageArea,
    QualificationStatus,
    ServiceQualification,
)
from app.models.quote_mirror import (  # noqa: F401
    QuoteMirror,
    QuoteSyncState,
)
from app.models.radius import (  # noqa: F401
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.radius_active_session import RadiusActiveSession  # noqa: F401
from app.models.radius_error import (  # noqa: F401
    RadiusAuthError,
    RadiusAuthErrorType,
)
from app.models.rbac import (  # noqa: F401
    Permission,
    Role,
    RolePermission,
    SubscriberPermission,
    SubscriberRole,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.referral import (  # noqa: F401
    ReferralMirror,
    ReferralProgramCache,
)
from app.models.referral_native import (  # noqa: F401
    Referral,
    ReferralCode,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.router_management import (  # noqa: F401
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterInterface,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
    RouterTemplateCategory,
)
from app.models.sales import (  # noqa: F401
    Lead,
    LeadCaptureMethod,
    LeadOriginCapture,
    LeadSourcePlatform,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteLineItem,
    QuoteStatus,
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.scheduler import ScheduledTask, ScheduleType  # noqa: F401
from app.models.sequence import DocumentSequence  # noqa: F401
from app.models.service_extension import (  # noqa: F401
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.service_request import (  # noqa: F401
    ResellerServiceRequest,
    Serviceability,
    ServiceRequestStatus,
)
from app.models.service_team import (  # noqa: F401
    ServiceTeam,
    ServiceTeamMember,
    ServiceTeamMemberRole,
    ServiceTeamType,
)
from app.models.snmp import (  # noqa: F401
    SnmpAuthProtocol,
    SnmpCredential,
    SnmpOid,
    SnmpPoller,
    SnmpPrivProtocol,
    SnmpReading,
    SnmpTarget,
    SnmpVersion,
)
from app.models.splynx_transaction import SplynxBillingTransaction  # noqa: F401
from app.models.stored_file import StoredFile  # noqa: F401
from app.models.subscriber import (  # noqa: F401
    Address,
    AddressType,
    ChannelType,
    ContactMethod,
    Gender,
    PartyStatus,
    Reseller,
    Subscriber,
    SubscriberChannel,
    SubscriberContact,
    SubscriberCustomField,
    SubscriberStatus,
)
from app.models.subscriber_field_verification import (  # noqa: F401
    SubscriberFieldVerification,
)
from app.models.subscription_billing_treatment import (  # noqa: F401
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
    SubscriptionBillingGrant,
    SubscriptionBillingTreatment,
)
from app.models.subscription_change import (  # noqa: F401
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_engine import (  # noqa: F401
    SettingValueType,
    SubscriptionEngine,
    SubscriptionEngineSetting,
)
from app.models.subscription_lifecycle_schedule import (  # noqa: F401
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.models.support import (  # noqa: F401
    Ticket,
    TicketAccessToken,
    TicketAssignee,
    TicketChannel,
    TicketComment,
    TicketCommentAuthorType,
    TicketLink,
    TicketMerge,
    TicketPriority,
    TicketSlaEvent,
    TicketStatus,
)
from app.models.system_user import SystemUser  # noqa: F401
from app.models.table_column_config import TableColumnConfig  # noqa: F401
from app.models.table_column_default_config import (  # noqa: F401
    TableColumnDefaultConfig,
)
from app.models.task_execution import (  # noqa: F401
    TaskExecution,
    TaskExecutionStatus,
)
from app.models.team_inbox import (  # noqa: F401
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxChannelType,
    InboxComment,
    InboxContactLink,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxLabel,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
    InboxMessageTemplate,
    InboxReplyMacro,
    InboxSavedFilter,
    InboxTeamRole,
    InboxTeamSource,
    TeamInboxEmailRoute,
)
from app.models.ticket_workflow import (  # noqa: F401
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    SlaTarget,
    TicketAssignmentCounter,
    TicketAssignmentRule,
    TicketAssignmentStrategy,
    WorkflowEntityType,
)
from app.models.tr069 import (  # noqa: F401
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Event,
    Tr069Job,
    Tr069JobStatus,
    Tr069Parameter,
    Tr069Session,
)
from app.models.uisp_control import (  # noqa: F401
    UispConfigSnapshot,
    UispDeviceIntent,
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)
from app.models.usage import (  # noqa: F401
    AccountingStatus,
    QuotaBucket,
    RadiusAccountingSession,
    SubscriberDailyUsage,
    UsageCharge,
    UsageChargeStatus,
    UsageRatingRun,
    UsageRatingRunStatus,
    UsageRecord,
    UsageSource,
)
from app.models.vendor_routes import (  # noqa: F401
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteReviewEvent,
    AsBuiltRouteReviewEventImmutableError,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectLifecycleEventImmutableError,
    InstallationProjectNote,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    VariationType,
    Vendor,
    VendorAssignmentType,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
    VendorPurchaseInvoiceStatus,
    VendorUser,
)
from app.models.wireguard import (  # noqa: F401
    WireGuardConnectionLog,
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServer,
)
from app.models.work_link import (  # noqa: F401
    WorkEntityType,
    WorkLink,
    WorkLinkType,
)
from app.models.work_order import WorkOrder  # noqa: F401
from app.models.workqueue import WorkqueueItemKind, WorkqueueSnooze  # noqa: F401
