# ac.dashboard.support_view  (requirements: requirement.support_dashboard; operations: operation.ticket.stats)
Feature: View support dashboard succeeds

  Scenario: View support dashboard succeeds
    Given the user is signed in as Manager
    When Get ticket statistics
    Then The manager sees ticket statistics
